import os
from datetime import datetime, timedelta
from uuid import uuid4

from flask import Blueprint, current_app, jsonify, request, send_from_directory
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from extensions import db
from models import (
    ActivityMedia, ActivityParticipant, ClubActivity, ClubArchive, Department,
    Equipment, EquipmentCategory, EquipmentUsage, File, OperationRecord,
    PointTransaction, Role, SocialAccount, TemporaryDepartmentGrant, User,
)
from utils.auth_helper import check_file_access, get_current_user, require_auth
from utils.file_helper import sanitize_original_name


club_bp = Blueprint('club', __name__, url_prefix='/api/club')
ALLOWED_COVER_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp', 'gif'}
# 设备台账状态。'报废' 不计入首页的「在库装备」数量，且不可领用。
ALLOWED_EQUIPMENT_STATUS = ('正常', '维修中', '报废', '已遗失')


def _payload():
    """同时支持 JSON 和带封面图片的 multipart/form-data。"""
    return request.form.to_dict() if request.content_type and 'multipart/form-data' in request.content_type else (request.get_json() or {})


def _save_cover(kind):
    image = request.files.get('cover')
    if not image or not image.filename:
        return None
    # 只取扩展名做白名单校验，落盘一律使用 UUID 文件名，因此中文原名不会造成问题。
    _, ext = sanitize_original_name(image.filename)
    if ext not in ALLOWED_COVER_EXTENSIONS:
        raise ValueError('仅支持 jpg、jpeg、png、webp、gif 图片')
    folder = os.path.join(current_app.config['UPLOAD_FOLDER'], 'covers', kind)
    os.makedirs(folder, exist_ok=True)
    filename = f'{uuid4().hex}.{ext}'
    target = os.path.join(folder, filename)
    image.save(target)
    if os.path.getsize(target) > 10 * 1024 * 1024:
        os.remove(target)
        raise ValueError('封面图片不能超过 10MB')
    return filename


def _discard_cover(kind, filename):
    """替换或删除封面后清理旧图片，避免 uploads 目录堆积孤儿文件。

    必须在数据库事务提交成功之后调用：否则事务回滚了、图片却已删除，
    会造成数据库记录与磁盘文件不一致。
    """
    if not filename:
        return
    path = os.path.join(current_app.config['UPLOAD_FOLDER'], 'covers', kind, filename)
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        current_app.logger.warning('旧封面清理失败: %s', path)


def _resolve_equipment_category(value):
    """把请求里的分类标识解析成 EquipmentCategory，非法输入一律返回 None。

    直接用 Query.get(None) 会抛 InvalidRequestError 变成 500，这里统一收敛成
    「查不到就是 None」，让调用方返回 4xx。
    """
    try:
        category_id = int(value)
    except (TypeError, ValueError):
        return None
    return db.session.get(EquipmentCategory, category_id)


def _resolve_int(value, fallback=None):
    """安全地把请求参数转成整数，失败时返回 fallback 而不是抛异常。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _as_text(value):
    """把请求字段安全地转成去掉首尾空白的字符串。

    浏览器表单提交的 multipart 字段一定是字符串，但直接调 JSON 接口可以传数字、
    数组或对象，这时 `(value or '').strip()` 会抛 AttributeError 变成 500。
    统一走这个函数：标量转成字符串，容器类型当作空值，交给后续的必填校验去返回 4xx。
    """
    if value is None:
        return ''
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value).strip()
    return ''


@club_bp.route('/covers/<string:kind>/<path:filename>', methods=['GET'])
def get_cover(kind, filename):
    if kind not in ('activity', 'equipment'):
        return jsonify(msg='图片类型不存在'), 404
    folder = os.path.join(current_app.config['UPLOAD_FOLDER'], 'covers', kind)
    return send_from_directory(folder, filename)


def _parse_dt(value, required=False):
    if not value:
        if required:
            raise ValueError('缺少日期时间')
        return None
    return datetime.fromisoformat(str(value).replace('Z', '+00:00')).replace(tzinfo=None)


def _is_captain(user):
    return user and (user.has_role('captain') or user.has_role('admin'))


def _is_minister(user, department_name=None):
    if not user or not user.has_role('minister'):
        return False
    return department_name is None or (user.department and user.department.department_name == department_name)


def _has_temporary_edit(user, department_name):
    if not user:
        return False
    now = datetime.now()
    return TemporaryDepartmentGrant.query.join(Department).filter(
        TemporaryDepartmentGrant.user_id == user.user_id,
        Department.department_name == department_name,
        TemporaryDepartmentGrant.revoked_at.is_(None),
        TemporaryDepartmentGrant.starts_at <= now,
        TemporaryDepartmentGrant.expires_at > now,
    ).first() is not None


def _can_department_edit(user, department_name):
    return _is_captain(user) or _is_minister(user, department_name) or _has_temporary_edit(user, department_name)


def _can_manage_activity(user, activity):
    return bool(user and (_is_captain(user) or activity.lead_instructor_id == user.user_id or
                          _can_department_edit(user, '教官部')))


def _deny_unless(condition, message='权限不足'):
    return None if condition else (jsonify(msg=message), 403)


def _record(user, module, action, target_type, target=None, target_name=None, detail=None):
    """在当前事务中保存统一操作时间记录。"""
    target_id = None
    if target is not None:
        target_id = getattr(target, f'{target_type}_id', None)
        if target_id is None:
            for key in ('activity_id', 'equipment_id', 'media_id', 'archive_id',
                        'usage_id', 'category_id', 'account_id', 'participant_id'):
                if hasattr(target, key):
                    target_id = getattr(target, key)
                    break
    db.session.add(OperationRecord(
        module=module, action=action, target_type=target_type,
        target_id=target_id, target_name=target_name,
        operator_id=user.user_id if user else None, detail=detail,
        occurred_at=datetime.now(),
    ))


def _award_activity_points(activity, operator):
    """结项时向有效出勤人员发放一次参与积分，返回发放人数。"""
    awarded_count = 0
    points = activity.participation_points or 0
    for participant in activity.participants:
        if participant.attendance_status not in ('已签到', '已到'):
            continue
        existing = PointTransaction.query.filter_by(
            user_id=participant.user_id, activity_id=activity.activity_id).first()
        if existing:
            continue
        participant.user.points = (participant.user.points or 0) + points
        participant.points_awarded = points
        db.session.add(PointTransaction(
            user_id=participant.user_id, activity_id=activity.activity_id,
            points=points, reason=f'参与活动：{activity.title}',
            created_by=operator.user_id,
        ))
        awarded_count += 1
    return awarded_count


@club_bp.route('/departments', methods=['GET'])
def departments():
    rows = Department.query.order_by(Department.sort_order).all()
    return jsonify(departments=[d.to_dict() for d in rows]), 200


@club_bp.route('/members', methods=['GET'])
@require_auth
def members():
    department_id = request.args.get('department_id', type=int)
    query = User.query.filter(User.status.in_((1, 3)))
    if department_id:
        query = query.filter_by(department_id=department_id)
    rows = query.order_by(User.real_name, User.username).all()
    return jsonify(members=[u.to_dict() for u in rows]), 200


@club_bp.route('/overview', methods=['GET'])
@require_auth
def overview():
    now = datetime.now()
    active_borrows = EquipmentUsage.query.filter_by(status='使用中').count()
    pending_media = ActivityMedia.query.filter_by(review_status='待审阅').count()
    # 已终止的项目不该再出现在「近期活动」里，口径与活动课表保持一致。
    # activity_count 仍统计全部项目，这样首页数字和活动项目页「全部」标签下的
    # 「N 个项目」能对上。
    upcoming_query = ClubActivity.query.filter(
        ClubActivity.start_time >= now, ClubActivity.status != '已终止')
    upcoming = upcoming_query.order_by(ClubActivity.start_time).limit(4).all()
    return jsonify(
        member_count=User.query.filter(User.status.in_((1, 3))).count(),
        activity_count=ClubActivity.query.count(),
        upcoming_count=upcoming_query.count(),
        # 已报废的设备不再计入「在库装备」
        equipment_count=db.session.query(func.sum(Equipment.total_quantity)).filter(
            Equipment.status != '报废').scalar() or 0,
        active_borrows=active_borrows,
        pending_media=pending_media,
        upcoming=[a.to_dict() for a in upcoming],
    ), 200


# ── 时间中心：日程与统一操作记录 ─────────────────────────────────────────────

@club_bp.route('/time/overview', methods=['GET'])
@require_auth
def time_overview():
    now = datetime.now()
    try:
        start = _parse_dt(request.args.get('start')) or now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = _parse_dt(request.args.get('end')) or (start.replace(hour=23, minute=59, second=59) + timedelta(days=30))
    except ValueError:
        return jsonify(msg='时间范围格式错误'), 400

    events = []
    # 时间中心的日程同样排除已终止项目，否则取消掉的活动仍会被当成待办提醒。
    activities = ClubActivity.query.filter(
        ClubActivity.start_time >= start, ClubActivity.start_time <= end,
        ClubActivity.status != '已终止',
    ).order_by(ClubActivity.start_time).all()
    for row in activities:
        events.append({
            'module': 'activity', 'event_type': '活动安排', 'target_id': row.activity_id,
            'title': row.title, 'time': row.start_time.isoformat(),
            'end_time': row.end_time.isoformat() if row.end_time else None,
            'status': row.status, 'detail': f'{row.activity_type} · {row.location or "地点待定"}',
            'is_overdue': bool(row.end_time and row.end_time < now and row.status != '已完成'),
        })

    usages = EquipmentUsage.query.filter(
        EquipmentUsage.status == '使用中', EquipmentUsage.expected_return_at.isnot(None),
        EquipmentUsage.expected_return_at >= start, EquipmentUsage.expected_return_at <= end,
    ).order_by(EquipmentUsage.expected_return_at).all()
    for row in usages:
        events.append({
            'module': 'equipment', 'event_type': '设备应归还', 'target_id': row.usage_id,
            'title': f'{row.equipment.equipment_name} × {row.quantity}',
            'time': row.expected_return_at.isoformat(), 'end_time': None,
            'status': row.status, 'detail': f'领用人：{row.user.real_name or row.user.username}',
            'is_overdue': row.expected_return_at < now,
        })

    pending_media = ActivityMedia.query.filter(
        ActivityMedia.review_status == '待审阅', ActivityMedia.submitted_at <= end
    ).order_by(ActivityMedia.submitted_at).all()
    for row in pending_media:
        if row.submitted_at >= start:
            events.append({
                'module': 'media', 'event_type': '素材待审', 'target_id': row.media_id,
                'title': row.activity.title, 'time': row.submitted_at.isoformat(),
                'end_time': None, 'status': row.review_status,
                'detail': f'提交人：{row.submitter.real_name or row.submitter.username}',
                'is_overdue': (now - row.submitted_at).days >= 3,
            })

    events.sort(key=lambda item: item['time'])
    return jsonify(
        start=start.isoformat(), end=end.isoformat(), events=events,
        activity_count=len(activities),
        return_due_count=len(usages),
        pending_media_count=len(pending_media),
        overdue_count=sum(1 for item in events if item['is_overdue']),
    ), 200


@club_bp.route('/time/records', methods=['GET'])
@require_auth
def time_records():
    module = (request.args.get('module') or '').strip()
    limit = min(max(request.args.get('limit', 50, type=int), 1), 200)
    query = OperationRecord.query
    if module:
        query = query.filter_by(module=module)
    try:
        start = _parse_dt(request.args.get('start'))
        end = _parse_dt(request.args.get('end'))
    except ValueError:
        return jsonify(msg='时间范围格式错误'), 400
    if start:
        query = query.filter(OperationRecord.occurred_at >= start)
    if end:
        query = query.filter(OperationRecord.occurred_at <= end)
    rows = query.order_by(OperationRecord.occurred_at.desc()).limit(limit).all()
    return jsonify(records=[row.to_dict() for row in rows]), 200


@club_bp.route('/schedule', methods=['GET'])
@require_auth
def activity_schedule():
    """课程表式活动日程，默认返回当前周起四周。"""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    monday = today - timedelta(days=today.weekday())
    try:
        start = _parse_dt(request.args.get('start')) or monday
        end = _parse_dt(request.args.get('end')) or (start + timedelta(days=28))
    except ValueError:
        return jsonify(msg='时间范围格式错误'), 400
    rows = ClubActivity.query.filter(
        ClubActivity.start_time >= start, ClubActivity.start_time < end,
        ClubActivity.status != '已终止',
    ).order_by(ClubActivity.start_time).all()
    return jsonify(start=start.isoformat(), end=end.isoformat(),
                   activities=[row.to_dict() for row in rows]), 200


@club_bp.route('/points/my', methods=['GET'])
@require_auth
def my_points():
    user = get_current_user()
    rows = PointTransaction.query.filter_by(user_id=user.user_id).order_by(
        PointTransaction.created_at.desc()).all()
    return jsonify(points=user.points or 0,
                   transactions=[row.to_dict() for row in rows]), 200


# ── 教官部：活动项目 ─────────────────────────────────────────────────────────

@club_bp.route('/activities', methods=['GET'])
@require_auth
def list_activities():
    status = (request.args.get('status') or '').strip()
    query = ClubActivity.query
    if status:
        query = query.filter_by(status=status)
    rows = query.order_by(ClubActivity.start_time.desc()).all()
    return jsonify(activities=[a.to_dict() for a in rows]), 200


@club_bp.route('/activities', methods=['POST'])
@require_auth
def create_activity():
    user = get_current_user()
    denied = _deny_unless(_can_department_edit(user, '教官部'), '仅教官部授权人员或队长可开设活动')
    if denied:
        return denied
    data = _payload()
    title = (data.get('title') or '').strip()
    if not title:
        return jsonify(msg='活动名称不能为空'), 400
    try:
        start_time = _parse_dt(data.get('start_time'), required=True)
        end_time = _parse_dt(data.get('end_time'))
        checkin_start = _parse_dt(data.get('checkin_start')) or (start_time - timedelta(minutes=30))
        checkin_end = _parse_dt(data.get('checkin_end')) or (start_time + timedelta(minutes=30))
    except ValueError:
        return jsonify(msg='日期时间格式错误'), 400
    if end_time and end_time <= start_time:
        return jsonify(msg='活动结束时间必须晚于开始时间'), 400
    if checkin_end <= checkin_start:
        return jsonify(msg='签到结束时间必须晚于签到开始时间'), 400
    try:
        participation_points = int(data.get('participation_points', 0))
    except (TypeError, ValueError):
        return jsonify(msg='活动积分必须是整数'), 400
    if participation_points < 0 or participation_points > 1000:
        return jsonify(msg='活动积分必须在 0 到 1000 之间'), 400
    lead_id = data.get('lead_instructor_id') or user.user_id
    lead = User.query.get(lead_id)
    if not lead:
        return jsonify(msg='活动主教官不存在'), 404
    try:
        cover_image = _save_cover('activity')
    except ValueError as error:
        return jsonify(msg=str(error)), 415
    activity = ClubActivity(
        title=title, activity_type=data.get('activity_type', '升旗'),
        description=data.get('description'), location=data.get('location'),
        start_time=start_time, end_time=end_time, status=data.get('status', '筹备中'),
        created_by=user.user_id, lead_instructor_id=lead.user_id,
        cover_image=cover_image,
        checkin_start=checkin_start, checkin_end=checkin_end,
        participation_points=participation_points,
    )
    db.session.add(activity)
    db.session.flush()
    _record(user, 'activity', '创建活动', 'activity', activity, activity.title,
            f'{activity.activity_type} · {activity.start_time.isoformat()}')
    db.session.commit()
    return jsonify(msg='活动项目已创建', activity=activity.to_dict()), 201


@club_bp.route('/activities/<int:activity_id>', methods=['GET'])
@require_auth
def activity_detail(activity_id):
    activity = ClubActivity.query.get_or_404(activity_id)
    return jsonify(activity=activity.to_dict(include_participants=True),
                   media=[m.to_dict() for m in activity.media_submissions]), 200


@club_bp.route('/activities/<int:activity_id>/record', methods=['GET'])
@require_auth
def activity_record(activity_id):
    activity = ClubActivity.query.get_or_404(activity_id)
    if activity.status not in ('已完成', '已终止') and (
            not activity.end_time or datetime.now() < activity.end_time):
        return jsonify(msg='活动尚未结束，暂未生成最终记录'), 409
    participants = [row.to_dict() for row in activity.participants]
    status_counts = {}
    scores = []
    for row in activity.participants:
        status_counts[row.attendance_status] = status_counts.get(row.attendance_status, 0) + 1
        if row.score is not None:
            scores.append(float(row.score))
    return jsonify(
        activity=activity.to_dict(), participants=participants,
        attendance_summary=status_counts,
        average_score=round(sum(scores) / len(scores), 2) if scores else None,
        total_points_awarded=sum(row.points_awarded or 0 for row in activity.participants),
    ), 200


@club_bp.route('/activities/<int:activity_id>', methods=['PUT'])
@require_auth
def edit_activity(activity_id):
    user = get_current_user()
    activity = ClubActivity.query.get_or_404(activity_id)
    denied = _deny_unless(_can_manage_activity(user, activity), '仅项目创建者、主教官或管理人员可编辑项目')
    if denied:
        return denied
    if activity.status == '已终止':
        return jsonify(msg='已终止项目不能编辑'), 409
    if datetime.now() >= activity.start_time:
        return jsonify(msg='活动已经开始，不能再修改项目安排'), 409
    data = request.get_json() or {}
    try:
        new_start = _parse_dt(data.get('start_time')) if 'start_time' in data else activity.start_time
        new_end = _parse_dt(data.get('end_time')) if 'end_time' in data else activity.end_time
        new_checkin_start = (_parse_dt(data.get('checkin_start'))
                             if 'checkin_start' in data else activity.checkin_start)
        new_checkin_end = (_parse_dt(data.get('checkin_end'))
                           if 'checkin_end' in data else activity.checkin_end)
    except ValueError:
        return jsonify(msg='日期时间格式错误'), 400
    if new_end and new_end <= new_start:
        return jsonify(msg='活动结束时间必须晚于开始时间'), 400
    if not new_checkin_start or not new_checkin_end or new_checkin_end <= new_checkin_start:
        return jsonify(msg='签到时间窗口设置无效'), 400
    if 'participation_points' in data:
        try:
            points = int(data['participation_points'])
        except (TypeError, ValueError):
            return jsonify(msg='活动积分必须是整数'), 400
        if points < 0 or points > 1000:
            return jsonify(msg='活动积分必须在 0 到 1000 之间'), 400
        if (points != activity.participation_points and
                activity.created_by != user.user_id and not _is_captain(user)):
            return jsonify(msg='仅项目创建者或队长可调整活动积分'), 403
        activity.participation_points = points
    for field in ('title', 'activity_type', 'description', 'location'):
        if field in data:
            setattr(activity, field, data[field])
    activity.start_time, activity.end_time = new_start, new_end
    activity.checkin_start, activity.checkin_end = new_checkin_start, new_checkin_end
    _record(user, 'activity', '编辑活动项目', 'activity', activity, activity.title,
            f'活动时间：{activity.start_time.isoformat()}')
    db.session.commit()
    return jsonify(msg='活动项目已更新', activity=activity.to_dict()), 200


@club_bp.route('/activities/<int:activity_id>/terminate', methods=['POST'])
@require_auth
def terminate_activity(activity_id):
    user = get_current_user()
    activity = ClubActivity.query.get_or_404(activity_id)
    denied = _deny_unless(_can_manage_activity(user, activity), '无权终止该项目')
    if denied:
        return denied
    if activity.status in ('已完成', '已终止'):
        return jsonify(msg='项目已经结束'), 409
    data = request.get_json() or {}
    reason = (data.get('reason') or '').strip()
    if not reason:
        return jsonify(msg='请填写终止原因'), 400
    activity.status = '已终止'
    activity.terminated_at = datetime.now()
    activity.termination_reason = reason
    _record(user, 'activity', '人为终止项目', 'activity', activity, activity.title, reason)
    db.session.commit()
    return jsonify(msg='活动项目已终止', activity=activity.to_dict()), 200


@club_bp.route('/activities/<int:activity_id>/check-in', methods=['POST'])
@require_auth
def self_check_in(activity_id):
    user = get_current_user()
    activity = ClubActivity.query.get_or_404(activity_id)
    now = datetime.now()
    if activity.status in ('已完成', '已终止'):
        return jsonify(msg='项目已经结束，不能打卡'), 409
    if not activity.checkin_start or not activity.checkin_end:
        return jsonify(msg='项目尚未设置签到时间'), 409
    if now < activity.checkin_start:
        return jsonify(msg=f'签到尚未开始，请于 {activity.checkin_start:%m月%d日 %H:%M} 后打卡'), 409
    if now > activity.checkin_end:
        return jsonify(msg='签到时间已结束，本次打卡无效'), 409
    row = ActivityParticipant.query.filter_by(activity_id=activity_id, user_id=user.user_id).first()
    if row and row.attendance_status in ('已签到', '已到'):
        return jsonify(msg='你已经完成签到', participant=row.to_dict()), 200
    if not row:
        row = ActivityParticipant(activity_id=activity_id, user_id=user.user_id)
        db.session.add(row)
    row.attendance_status = '已签到'
    row.checked_at = now
    row.checkin_method = '本人打卡'
    db.session.flush()
    _record(user, 'activity', '活动签到', 'participant', row,
            f'{activity.title} / {user.real_name or user.username}',
            f'签到时间：{now.isoformat()}')
    db.session.commit()
    return jsonify(msg='签到成功，已计入参与名单', participant=row.to_dict()), 200


@club_bp.route('/activities/<int:activity_id>/transfer', methods=['PUT'])
@require_auth
def transfer_activity(activity_id):
    user = get_current_user()
    activity = ClubActivity.query.get_or_404(activity_id)
    denied = _deny_unless(_is_captain(user) or _is_minister(user, '教官部'), '仅教官部部长或队长可移交项目')
    if denied:
        return denied
    target = User.query.get((request.get_json() or {}).get('lead_instructor_id'))
    if not target or target.status != 1:
        return jsonify(msg='目标用户不存在或已停用'), 404
    activity.lead_instructor_id = target.user_id
    _record(user, 'activity', '移交主教官', 'activity', activity, activity.title,
            f'新主教官：{target.real_name or target.username}')
    db.session.commit()
    return jsonify(msg='活动主教官已移交', activity=activity.to_dict()), 200


@club_bp.route('/activities/<int:activity_id>/participants', methods=['PUT'])
@require_auth
def update_participant(activity_id):
    user = get_current_user()
    activity = ClubActivity.query.get_or_404(activity_id)
    denied = _deny_unless(_can_manage_activity(user, activity), '仅活动主教官、教官部部长或队长可登记考勤评分')
    if denied:
        return denied
    data = request.get_json() or {}
    target = User.query.get(data.get('user_id'))
    if not target:
        return jsonify(msg='社员不存在'), 404
    row = ActivityParticipant.query.filter_by(activity_id=activity_id, user_id=target.user_id).first()
    if not row:
        row = ActivityParticipant(activity_id=activity_id, user_id=target.user_id)
        db.session.add(row)
    if 'attendance_status' in data:
        row.attendance_status = data['attendance_status']
        row.checked_at = datetime.now()
        row.checkin_method = '管理登记'
    if 'score' in data:
        score = float(data['score'])
        if score < 0 or score > 100:
            return jsonify(msg='表现评分必须在 0 到 100 之间'), 400
        row.score = score
    if 'performance_comment' in data:
        row.performance_comment = data['performance_comment']
    db.session.flush()
    _record(user, 'activity', '保存考勤评分', 'participant', row,
            f'{activity.title} / {target.real_name or target.username}',
            f'{row.attendance_status} · {float(row.score) if row.score is not None else "未评分"}')
    db.session.commit()
    return jsonify(msg='考勤与表现已记录', participant=row.to_dict()), 200


@club_bp.route('/activities/<int:activity_id>/summary', methods=['PUT'])
@require_auth
def update_summary(activity_id):
    user = get_current_user()
    activity = ClubActivity.query.get_or_404(activity_id)
    denied = _deny_unless(_can_manage_activity(user, activity), '仅活动主教官可填写项目总结')
    if denied:
        return denied
    data = request.get_json() or {}
    activity.summary = (data.get('summary') or '').strip()
    awarded_count = 0
    if data.get('status'):
        if activity.status == '已终止':
            return jsonify(msg='已终止项目不能结项'), 409
        if data['status'] == '已完成' and datetime.now() < activity.start_time:
            return jsonify(msg='活动尚未开始，不能提前结项'), 409
        activity.status = data['status']
        if activity.status == '已完成':
            awarded_count = _award_activity_points(activity, user)
    _record(user, 'activity', '保存项目总结', 'activity', activity, activity.title,
            f'项目状态：{activity.status} · 发放积分人数：{awarded_count}')
    db.session.commit()
    return jsonify(msg='项目总结已保存', activity=activity.to_dict(),
                   awarded_count=awarded_count), 200


@club_bp.route('/activities/<int:activity_id>/media', methods=['POST'])
@require_auth
def submit_media(activity_id):
    user = get_current_user()
    activity = ClubActivity.query.get_or_404(activity_id)
    if activity.status == '已终止':
        return jsonify(msg='项目已终止，不能再提交素材'), 409
    data = request.get_json() or {}
    # file_id 缺失或不是整数时，Query.get(None) 会抛 InvalidRequestError 变成 500，
    # 这里先做类型收敛，保证任何畸形请求都只返回 4xx。
    try:
        file_id = int(data.get('file_id'))
    except (TypeError, ValueError):
        return jsonify(msg='缺少有效的文件标识'), 400
    media_file = db.session.get(File, file_id)
    if not media_file or media_file.status != 1:
        return jsonify(msg='请先上传素材文件'), 404
    if not check_file_access(media_file, user, 'read'):
        return jsonify(msg='无权使用该文件'), 403
    duplicate = ActivityMedia.query.filter_by(
        activity_id=activity.activity_id, file_id=media_file.file_id).first()
    if duplicate:
        return jsonify(msg='该文件已经提交过，请勿重复提交'), 409
    row = ActivityMedia(activity_id=activity.activity_id, file_id=media_file.file_id,
                        submitter_id=user.user_id)
    db.session.add(row)
    db.session.flush()
    _record(user, 'media', '提交活动素材', 'media', row, activity.title,
            media_file.original_name)
    db.session.commit()
    return jsonify(msg='素材已提交办公室审阅', media=row.to_dict()), 201


# ── 后勤部：设备与占用 ───────────────────────────────────────────────────────

@club_bp.route('/equipment/categories', methods=['GET', 'POST'])
@require_auth
def equipment_categories():
    if request.method == 'GET':
        roots = EquipmentCategory.query.filter_by(parent_id=None).order_by(
            EquipmentCategory.sort_order, EquipmentCategory.category_id).all()
        flat = EquipmentCategory.query.order_by(EquipmentCategory.sort_order,
                                                EquipmentCategory.category_id).all()
        return jsonify(categories=[r.to_dict(include_children=True) for r in roots],
                       flat_categories=[r.to_dict() for r in flat]), 200
    user = get_current_user()
    denied = _deny_unless(_can_department_edit(user, '后勤部'), '仅后勤部授权人员或队长可新增分类')
    if denied:
        return denied
    data = request.get_json() or {}
    name = (data.get('category_name') or '').strip()
    if not name:
        return jsonify(msg='分类名称不能为空'), 400
    parent_id = data.get('parent_id') or None
    if parent_id and not EquipmentCategory.query.get(parent_id):
        return jsonify(msg='父分类不存在'), 404
    duplicate = EquipmentCategory.query.filter_by(category_name=name, parent_id=parent_id).first()
    if duplicate:
        return jsonify(msg='同级分类名称已存在'), 409
    row = EquipmentCategory(category_name=name, description=data.get('description'),
                            parent_id=parent_id, sort_order=int(data.get('sort_order', 0)))
    db.session.add(row)
    db.session.flush()
    _record(user, 'equipment', '创建设备分类', 'category', row, row.full_name())
    db.session.commit()
    return jsonify(msg='设备分类已创建', category=row.to_dict()), 201


@club_bp.route('/equipment/categories/<int:category_id>', methods=['PUT', 'DELETE'])
@require_auth
def manage_equipment_category(category_id):
    user = get_current_user()
    denied = _deny_unless(_can_department_edit(user, '后勤部'), '仅后勤部授权人员或队长可管理分类')
    if denied:
        return denied
    row = EquipmentCategory.query.get_or_404(category_id)
    if request.method == 'DELETE':
        if row.children or row.equipment:
            return jsonify(msg='分类下仍有子分类或设备，不能删除'), 409
        _record(user, 'equipment', '删除设备分类', 'category', row, row.full_name())
        db.session.delete(row)
        db.session.commit()
        return jsonify(msg='设备分类已删除'), 200
    data = request.get_json() or {}
    if 'category_name' in data:
        row.category_name = (data['category_name'] or '').strip() or row.category_name
    if 'description' in data:
        row.description = data['description']
    if 'sort_order' in data:
        row.sort_order = int(data['sort_order'])
    if 'parent_id' in data:
        parent_id = data['parent_id'] or None
        if parent_id == row.category_id:
            return jsonify(msg='分类不能成为自己的父分类'), 400
        row.parent_id = parent_id
    try:
        _record(user, 'equipment', '更新设备分类', 'category', row, row.full_name())
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify(msg='同级分类名称已存在'), 409
    return jsonify(msg='设备分类已更新', category=row.to_dict()), 200


@club_bp.route('/equipment', methods=['GET', 'POST'])
@require_auth
def equipment_list():
    if request.method == 'GET':
        rows = Equipment.query.order_by(Equipment.equipment_name).all()
        return jsonify(equipment=[r.to_dict() for r in rows]), 200
    user = get_current_user()
    denied = _deny_unless(_can_department_edit(user, '后勤部'), '仅后勤部授权人员或队长可登记设备')
    if denied:
        return denied
    data = _payload()
    asset_code = _as_text(data.get('asset_code'))
    equipment_name = _as_text(data.get('equipment_name'))
    category = _resolve_equipment_category(data.get('category_id'))
    # 先做完所有校验再落盘封面，避免请求被拒后在 uploads 里留下孤儿图片
    if not asset_code or not equipment_name or category is None:
        return jsonify(msg='资产编号、设备名称和有效分类均为必填项'), 400
    qty = _resolve_int(data.get('total_quantity', 1))
    if qty is None:
        return jsonify(msg='设备数量必须是整数'), 400
    if qty < 1:
        return jsonify(msg='设备数量至少为 1'), 400
    status = _as_text(data.get('status')) or '正常'
    if status not in ALLOWED_EQUIPMENT_STATUS:
        return jsonify(msg=f'设备状态只能是 {"、".join(ALLOWED_EQUIPMENT_STATUS)}'), 400
    try:
        cover_image = _save_cover('equipment')
    except ValueError as error:
        return jsonify(msg=str(error)), 415
    row = Equipment(asset_code=asset_code, equipment_name=equipment_name,
                    category_id=category.category_id, total_quantity=qty,
                    available_quantity=qty,
                    storage_location=_as_text(data.get('storage_location')) or None,
                    notes=_as_text(data.get('notes')) or None,
                    status=status, cover_image=cover_image)
    db.session.add(row)
    try:
        db.session.flush()
        _record(user, 'equipment', '登记设备', 'equipment', row, row.equipment_name,
                f'资产编号：{row.asset_code} · 数量：{row.total_quantity}')
        db.session.commit()
    except IntegrityError:
        # asset_code 是唯一约束，重复登记原本会抛 500
        db.session.rollback()
        _discard_cover('equipment', cover_image)
        return jsonify(msg=f'资产编号 {asset_code} 已存在'), 409
    return jsonify(msg='设备已登记', equipment=row.to_dict()), 201


@club_bp.route('/equipment/<int:equipment_id>', methods=['PUT', 'DELETE'])
@require_auth
def manage_equipment(equipment_id):
    user = get_current_user()
    denied = _deny_unless(_can_department_edit(user, '后勤部'), '仅后勤部授权人员或队长可管理设备')
    if denied:
        return denied
    row = Equipment.query.get_or_404(equipment_id)
    occupied = row.total_quantity - row.available_quantity

    if request.method == 'DELETE':
        if occupied > 0:
            return jsonify(msg=f'仍有 {occupied} 件在外未归还，请先完成归还'), 409
        if EquipmentUsage.query.filter_by(equipment_id=row.equipment_id).count():
            return jsonify(msg='该设备已有领用历史，为保留台账记录不能删除；请把状态改为「报废」'), 409
        name, code, cover = row.equipment_name, row.asset_code, row.cover_image
        _record(user, 'equipment', '删除设备', 'equipment', row, name, f'资产编号：{code}')
        db.session.delete(row)
        db.session.commit()
        _discard_cover('equipment', cover)
        return jsonify(msg='设备已删除'), 200

    data = _payload()
    # 先把所有新值算出来、全部校验通过，再统一写回 row。这样中途 return 时 row
    # 仍是原样，不会在会话里留下半截修改（脏对象一旦被后续 commit 带出去，
    # 就会以 NOT NULL / 唯一约束冲突的形式在别处爆掉）。
    pending = {}
    if 'total_quantity' in data:
        new_total = _resolve_int(data.get('total_quantity'))
        if new_total is None:
            return jsonify(msg='设备数量必须是整数'), 400
        if new_total < 1:
            return jsonify(msg='设备数量至少为 1'), 400
        if new_total < occupied:
            return jsonify(msg=f'当前有 {occupied} 件在外未归还，总量不能少于该数量'), 409
        # 调整总量时保持「在外占用数」不变，只改可用量
        pending['total_quantity'] = new_total
        pending['available_quantity'] = new_total - occupied
    if 'category_id' in data:
        category = _resolve_equipment_category(data.get('category_id'))
        if category is None:
            return jsonify(msg='设备分类不存在'), 404
        pending['category_id'] = category.category_id
    if 'status' in data:
        status = _as_text(data.get('status'))
        if status not in ALLOWED_EQUIPMENT_STATUS:
            return jsonify(msg=f'设备状态只能是 {"、".join(ALLOWED_EQUIPMENT_STATUS)}'), 400
        pending['status'] = status
    for field in ('asset_code', 'equipment_name', 'storage_location', 'notes'):
        if field in data:
            pending[field] = _as_text(data[field])
    if (not pending.get('asset_code', row.asset_code)
            or not pending.get('equipment_name', row.equipment_name)):
        return jsonify(msg='资产编号和设备名称不能为空'), 400
    try:
        new_cover = _save_cover('equipment')
    except ValueError as error:
        return jsonify(msg=str(error)), 415
    replaced_cover = row.cover_image if new_cover else None
    if new_cover:
        pending['cover_image'] = new_cover
    attempted_code = pending.get('asset_code', row.asset_code)
    for field, value in pending.items():
        setattr(row, field, value)
    try:
        _record(user, 'equipment', '更新设备台账', 'equipment', row, row.equipment_name,
                f'资产编号：{row.asset_code} · 总量：{row.total_quantity} · 状态：{row.status}')
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        _discard_cover('equipment', new_cover)
        return jsonify(msg=f'资产编号 {attempted_code} 已存在'), 409
    _discard_cover('equipment', replaced_cover)
    return jsonify(msg='设备信息已更新', equipment=row.to_dict()), 200


@club_bp.route('/equipment/usages', methods=['GET'])
@require_auth
def equipment_usages():
    only_active = request.args.get('active') == '1'
    query = EquipmentUsage.query
    if only_active:
        query = query.filter_by(status='使用中')
    rows = query.order_by(EquipmentUsage.borrowed_at.desc()).all()
    return jsonify(usages=[r.to_dict() for r in rows]), 200


@club_bp.route('/equipment/<int:equipment_id>/borrow', methods=['POST'])
@require_auth
def borrow_equipment(equipment_id):
    user = get_current_user()
    equipment = Equipment.query.get_or_404(equipment_id)
    if equipment.status != '正常':
        return jsonify(msg=f'该设备当前状态为「{equipment.status}」，不可领用'), 409
    data = request.get_json() or {}
    qty = _resolve_int(data.get('quantity', 1))
    if qty is None:
        return jsonify(msg='领用数量必须是整数'), 400
    if qty < 1 or qty > equipment.available_quantity:
        return jsonify(msg=f'设备占用冲突：当前仅剩 {equipment.available_quantity} 件可用'), 409
    try:
        expected = _parse_dt(data.get('expected_return_at'))
    except ValueError:
        return jsonify(msg='预计归还时间格式错误'), 400
    row = EquipmentUsage(equipment_id=equipment_id, user_id=user.user_id,
                         activity_id=data.get('activity_id'), quantity=qty,
                         expected_return_at=expected, notes=data.get('notes'))
    equipment.available_quantity -= qty
    db.session.add(row)
    db.session.flush()
    _record(user, 'equipment', '领用设备', 'usage', row, equipment.equipment_name,
            f'数量：{qty} · 预计归还：{expected.isoformat() if expected else "未设置"}')
    db.session.commit()
    return jsonify(msg='设备领用成功', usage=row.to_dict(), equipment=equipment.to_dict()), 201


@club_bp.route('/equipment/usages/<int:usage_id>/return', methods=['PUT'])
@require_auth
def return_equipment(usage_id):
    user = get_current_user()
    row = EquipmentUsage.query.get_or_404(usage_id)
    denied = _deny_unless(row.user_id == user.user_id or _can_department_edit(user, '后勤部'), '无权归还该设备')
    if denied:
        return denied
    if row.status != '使用中':
        return jsonify(msg='该记录已经归还'), 409
    row.status = '已归还'
    row.returned_at = datetime.now()
    row.equipment.available_quantity = min(row.equipment.total_quantity,
                                           row.equipment.available_quantity + row.quantity)
    _record(user, 'equipment', '归还设备', 'usage', row, row.equipment.equipment_name,
            f'归还数量：{row.quantity}')
    db.session.commit()
    return jsonify(msg='设备已归还', usage=row.to_dict()), 200


# ── 综合办公室：社媒、档案与素材审阅 ─────────────────────────────────────────

@club_bp.route('/office/socials', methods=['GET', 'POST'])
@require_auth
def socials():
    if request.method == 'GET':
        rows = SocialAccount.query.filter_by(status=1).all()
        return jsonify(accounts=[r.to_dict() for r in rows]), 200
    user = get_current_user()
    denied = _deny_unless(_can_department_edit(user, '综合办公室'), '仅综合办公室授权人员或队长可维护社媒账号')
    if denied:
        return denied
    data = request.get_json() or {}
    row = SocialAccount(platform=_as_text(data.get('platform')) or None,
                        account_name=_as_text(data.get('account_name')) or None,
                        link_url=_as_text(data.get('link_url')) or None,
                        icon=_as_text(data.get('icon')) or None)
    if not row.platform or not row.account_name or not row.link_url:
        return jsonify(msg='平台、账号名称和链接均不能为空'), 400
    db.session.add(row)
    db.session.flush()
    _record(user, 'office', '添加社媒账号', 'account', row, row.platform,
            row.account_name)
    db.session.commit()
    return jsonify(msg='社媒账号已添加', account=row.to_dict()), 201


@club_bp.route('/office/socials/<int:account_id>', methods=['PUT', 'DELETE'])
@require_auth
def manage_social(account_id):
    user = get_current_user()
    denied = _deny_unless(_can_department_edit(user, '综合办公室'), '仅综合办公室授权人员或队长可维护社媒账号')
    if denied:
        return denied
    row = SocialAccount.query.get_or_404(account_id)

    if request.method == 'DELETE':
        platform, account_name = row.platform, row.account_name
        _record(user, 'office', '删除社媒账号', 'account', row, platform, account_name)
        db.session.delete(row)
        db.session.commit()
        return jsonify(msg='社媒账号已删除'), 200

    data = request.get_json() or {}
    # 先算出合并后的完整值再校验，避免中途返回 400 时把 None 留在会话里
    merged = {field: getattr(row, field)
              for field in ('platform', 'account_name', 'link_url', 'icon')}
    for field in merged:
        if field in data:
            merged[field] = _as_text(data[field]) or None
    if not merged['platform'] or not merged['account_name'] or not merged['link_url']:
        return jsonify(msg='平台、账号名称和链接均不能为空'), 400
    for field, value in merged.items():
        setattr(row, field, value)
    _record(user, 'office', '更新社媒账号', 'account', row, row.platform, row.account_name)
    db.session.commit()
    return jsonify(msg='社媒账号已更新', account=row.to_dict()), 200


@club_bp.route('/office/archives', methods=['GET', 'POST'])
@require_auth
def archives():
    if request.method == 'GET':
        rows = ClubArchive.query.order_by(ClubArchive.archived_at.desc()).all()
        return jsonify(archives=[r.to_dict() for r in rows]), 200
    user = get_current_user()
    denied = _deny_unless(_can_department_edit(user, '综合办公室'), '仅综合办公室授权人员或队长可归档文件')
    if denied:
        return denied
    data = request.get_json() or {}
    # 同 submit_media：file_id 缺失或非法时先收敛类型，避免 Query.get(None) 抛 500。
    try:
        file_id = int(data.get('file_id'))
    except (TypeError, ValueError):
        return jsonify(msg='缺少有效的文件标识'), 400
    linked_file = db.session.get(File, file_id)
    if not linked_file:
        return jsonify(msg='请先上传需要归档的文件'), 404
    row = ClubArchive(title=_as_text(data.get('title')),
                      archive_type=_as_text(data.get('archive_type')) or '其他',
                      file_id=linked_file.file_id,
                      owner_id=user.user_id,
                      description=_as_text(data.get('description')) or None)
    if not row.title:
        return jsonify(msg='档案标题不能为空'), 400
    db.session.add(row)
    db.session.flush()
    _record(user, 'archive', '保存文件档案', 'archive', row, row.title,
            row.archive_type)
    db.session.commit()
    return jsonify(msg='文件已归档', archive=row.to_dict()), 201


@club_bp.route('/office/archives/<int:archive_id>', methods=['PUT', 'DELETE'])
@require_auth
def manage_archive(archive_id):
    user = get_current_user()
    denied = _deny_unless(_can_department_edit(user, '综合办公室'), '仅综合办公室授权人员或队长可管理档案')
    if denied:
        return denied
    row = ClubArchive.query.get_or_404(archive_id)

    if request.method == 'DELETE':
        # 只解除归档关系，不动底层文件：同一个文件可能还被活动素材引用。
        title, archive_type = row.title, row.archive_type
        _record(user, 'archive', '删除档案', 'archive', row, title, archive_type)
        db.session.delete(row)
        db.session.commit()
        return jsonify(msg='档案已删除，原始文件保留在文件库中'), 200

    data = request.get_json() or {}
    # 同 manage_equipment：全部校验通过后再写回，中途返回不留脏对象
    pending = {}
    if 'title' in data:
        pending['title'] = _as_text(data['title'])
    if not pending.get('title', row.title):
        return jsonify(msg='档案标题不能为空'), 400
    if 'archive_type' in data:
        archive_type = _as_text(data['archive_type'])
        if archive_type:
            pending['archive_type'] = archive_type
    if 'description' in data:
        pending['description'] = _as_text(data['description']) or None
    if 'file_id' in data:
        try:
            file_id = int(data['file_id'])
        except (TypeError, ValueError):
            return jsonify(msg='缺少有效的文件标识'), 400
        linked_file = db.session.get(File, file_id)
        if not linked_file:
            return jsonify(msg='请先上传需要归档的文件'), 404
        pending['file_id'] = linked_file.file_id
    for field, value in pending.items():
        setattr(row, field, value)
    _record(user, 'archive', '更新档案', 'archive', row, row.title, row.archive_type)
    db.session.commit()
    return jsonify(msg='档案已更新', archive=row.to_dict()), 200


@club_bp.route('/office/media', methods=['GET'])
@require_auth
def review_queue():
    rows = ActivityMedia.query.order_by(ActivityMedia.submitted_at.desc()).all()
    return jsonify(media=[r.to_dict() for r in rows]), 200


@club_bp.route('/office/media/<int:media_id>/review', methods=['PUT'])
@require_auth
def review_media(media_id):
    user = get_current_user()
    denied = _deny_unless(_can_department_edit(user, '综合办公室'), '仅综合办公室授权人员或队长可审阅素材')
    if denied:
        return denied
    data = request.get_json() or {}
    status = data.get('review_status')
    if status not in ('已通过', '需修改', '已归档'):
        return jsonify(msg='审阅状态无效'), 400
    row = ActivityMedia.query.get_or_404(media_id)
    row.review_status = status
    row.review_comment = data.get('review_comment')
    row.reviewer_id = user.user_id
    row.reviewed_at = datetime.now()
    _record(user, 'media', '审阅活动素材', 'media', row, row.activity.title,
            f'{status} · {row.review_comment or "无审阅意见"}')
    db.session.commit()
    return jsonify(msg='素材审阅结果已保存', media=row.to_dict()), 200
