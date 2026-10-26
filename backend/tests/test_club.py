import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from io import BytesIO

import bcrypt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from config import Config
from extensions import db
from models import ClubActivity, Department, Equipment, EquipmentCategory, Role, User


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    JWT_SECRET_KEY = 'test-key'


class ClubApiTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.upload_dir = tempfile.mkdtemp(prefix='ghd-test-')
        self.app.config['UPLOAD_FOLDER'] = self.upload_dir
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        department = Department(department_name='综合办公室', sort_order=1)
        captain_role = Role(role_name='captain')
        member_role = Role(role_name='member')
        password_hash = bcrypt.hashpw(b'password', bcrypt.gensalt()).decode()
        captain = User(username='captain', email='captain@test.local',
                       password_hash=password_hash, department=department)
        captain.roles.append(captain_role)
        member = User(username='member', email='member@test.local', real_name='测试社员',
                      password_hash=password_hash, department=department)
        member.roles.append(member_role)
        category = EquipmentCategory(category_name='仪仗装备')
        equipment = Equipment(asset_code='T-001', equipment_name='测试礼宾枪',
                              category=category, total_quantity=2, available_quantity=2)
        db.session.add_all([department, captain_role, member_role, captain, member, category, equipment])
        db.session.commit()
        self.client = self.app.test_client()
        response = self.client.post('/api/auth/login', json={'username': 'captain', 'password': 'password'})
        self.headers = {'Authorization': 'Bearer ' + response.get_json()['token']}
        member_response = self.client.post('/api/auth/login', json={
            'username': 'member', 'password': 'password'
        })
        self.member_headers = {'Authorization': 'Bearer ' + member_response.get_json()['token']}

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        shutil.rmtree(self.upload_dir, ignore_errors=True)

    def test_activity_creation_and_overview(self):
        response = self.client.post('/api/club/activities', headers=self.headers, data={
            'title': '周一升旗', 'activity_type': '升旗',
            'start_time': (datetime.now() + timedelta(days=1)).isoformat(),
            'location': '东区田径场',
            'cover': (BytesIO(b'fake-png-content'), 'activity.png'),
        }, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 201)
        cover_url = response.get_json()['activity']['cover_url']
        self.assertTrue(cover_url.endswith('.png'))
        cover_response = self.client.get(cover_url)
        self.assertEqual(cover_response.status_code, 200)
        cover_response.close()
        overview = self.client.get('/api/club/overview', headers=self.headers)
        self.assertEqual(overview.status_code, 200)
        self.assertEqual(overview.get_json()['activity_count'], 1)
        time_data = self.client.get('/api/club/time/overview', headers=self.headers,
                                    query_string={
                                        'start': datetime.now().isoformat(),
                                        'end': (datetime.now() + timedelta(days=7)).isoformat(),
                                    }).get_json()
        self.assertEqual(time_data['activity_count'], 1)
        records = self.client.get('/api/club/time/records', headers=self.headers).get_json()['records']
        self.assertEqual(records[0]['action'], '创建活动')

    def test_equipment_conflict_and_return(self):
        first = self.client.post('/api/club/equipment/1/borrow', headers=self.headers,
                                 json={'quantity': 2})
        self.assertEqual(first.status_code, 201)
        conflict = self.client.post('/api/club/equipment/1/borrow', headers=self.headers,
                                    json={'quantity': 1})
        self.assertEqual(conflict.status_code, 409)
        returned = self.client.put('/api/club/equipment/usages/1/return', headers=self.headers, json={})
        self.assertEqual(returned.status_code, 200)
        self.assertEqual(returned.get_json()['usage']['status'], '已归还')
        actions = [row['action'] for row in self.client.get(
            '/api/club/time/records?module=equipment', headers=self.headers
        ).get_json()['records']]
        self.assertIn('领用设备', actions)
        self.assertIn('归还设备', actions)

    def test_nested_equipment_categories(self):
        root = self.client.post('/api/club/equipment/categories', headers=self.headers,
                                json={'category_name': '宣传器材'})
        self.assertEqual(root.status_code, 201)
        root_id = root.get_json()['category']['category_id']
        child = self.client.post('/api/club/equipment/categories', headers=self.headers,
                                 json={'category_name': '摄影设备', 'parent_id': root_id})
        self.assertEqual(child.status_code, 201)
        tree = self.client.get('/api/club/equipment/categories', headers=self.headers).get_json()
        target = next(item for item in tree['categories'] if item['category_id'] == root_id)
        self.assertEqual(target['children'][0]['category_name'], '摄影设备')

    def test_equipment_cover_upload(self):
        response = self.client.post('/api/club/equipment', headers=self.headers, data={
            'asset_code': 'T-IMG-001', 'equipment_name': '带图设备',
            'category_id': '1', 'total_quantity': '1',
            'cover': (BytesIO(b'fake-jpeg-content'), 'equipment.jpg'),
        }, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 201)
        self.assertIn('/api/club/covers/equipment/',
                      response.get_json()['equipment']['cover_url'])

    def test_strict_checkin_points_and_final_record(self):
        now = datetime.now()
        created = self.client.post('/api/club/activities', headers=self.headers, json={
            'title': '严格考勤活动', 'activity_type': '训练',
            'start_time': (now + timedelta(hours=1)).isoformat(),
            'end_time': (now + timedelta(hours=2)).isoformat(),
            'checkin_start': (now + timedelta(minutes=30)).isoformat(),
            'checkin_end': (now + timedelta(minutes=45)).isoformat(),
            'participation_points': 25,
        })
        self.assertEqual(created.status_code, 201)
        activity_id = created.get_json()['activity']['activity_id']

        early = self.client.post(f'/api/club/activities/{activity_id}/check-in',
                                 headers=self.member_headers, json={})
        self.assertEqual(early.status_code, 409)

        activity = db.session.get(ClubActivity, activity_id)
        activity.start_time = now - timedelta(minutes=10)
        activity.end_time = now + timedelta(hours=1)
        activity.checkin_start = now - timedelta(minutes=5)
        activity.checkin_end = now + timedelta(minutes=5)
        db.session.commit()
        checked = self.client.post(f'/api/club/activities/{activity_id}/check-in',
                                   headers=self.member_headers, json={})
        self.assertEqual(checked.status_code, 200)
        self.assertEqual(checked.get_json()['participant']['checkin_method'], '本人打卡')

        completed = self.client.put(f'/api/club/activities/{activity_id}/summary',
                                    headers=self.headers,
                                    json={'summary': '测试结项', 'status': '已完成'})
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.get_json()['awarded_count'], 1)
        completed_again = self.client.put(f'/api/club/activities/{activity_id}/summary',
                                          headers=self.headers,
                                          json={'summary': '重复保存', 'status': '已完成'})
        self.assertEqual(completed_again.get_json()['awarded_count'], 0)

        points = self.client.get('/api/club/points/my', headers=self.member_headers).get_json()
        self.assertEqual(points['points'], 25)
        self.assertEqual(len(points['transactions']), 1)
        record = self.client.get(f'/api/club/activities/{activity_id}/record',
                                 headers=self.member_headers)
        self.assertEqual(record.status_code, 200)
        self.assertEqual(record.get_json()['participants'][0]['name'], '测试社员')
        self.assertEqual(record.get_json()['attendance_summary']['已签到'], 1)

        rejected = self.client.post(f'/api/club/activities/{activity_id}/check-in',
                                    headers=self.member_headers, json={})
        self.assertEqual(rejected.status_code, 409)

    def test_edit_terminate_and_week_schedule(self):
        now = datetime.now()
        start = now + timedelta(days=2)
        created = self.client.post('/api/club/activities', headers=self.headers, json={
            'title': '待调整活动', 'activity_type': '升旗',
            'start_time': start.isoformat(),
            'end_time': (start + timedelta(hours=1)).isoformat(),
            'participation_points': 10,
        })
        activity_id = created.get_json()['activity']['activity_id']
        edited = self.client.put(f'/api/club/activities/{activity_id}', headers=self.headers,
                                 json={'title': '已调整活动', 'participation_points': 18})
        self.assertEqual(edited.status_code, 200)
        self.assertEqual(edited.get_json()['activity']['participation_points'], 18)

        schedule_before = self.client.get('/api/club/schedule', headers=self.headers,
                                          query_string={
                                              'start': now.isoformat(),
                                              'end': (now + timedelta(days=7)).isoformat(),
                                          }).get_json()['activities']
        self.assertEqual(len(schedule_before), 1)
        terminated = self.client.post(f'/api/club/activities/{activity_id}/terminate',
                                      headers=self.headers, json={'reason': '天气原因取消'})
        self.assertEqual(terminated.status_code, 200)
        self.assertEqual(terminated.get_json()['activity']['status'], '已终止')
        schedule_after = self.client.get('/api/club/schedule', headers=self.headers,
                                         query_string={
                                             'start': now.isoformat(),
                                             'end': (now + timedelta(days=7)).isoformat(),
                                         }).get_json()['activities']
        self.assertEqual(schedule_after, [])


    # ── 第二梯队：设备/社媒/档案的编辑删除，以及已终止活动的统计口径 ──────────

    def test_equipment_update_and_delete(self):
        updated = self.client.put('/api/club/equipment/1', headers=self.headers, data={
            'equipment_name': '礼宾枪（新）', 'status': '维修中',
            'storage_location': '器材室 C 区', 'total_quantity': '5',
        }, content_type='multipart/form-data')
        self.assertEqual(updated.status_code, 200)
        payload = updated.get_json()['equipment']
        self.assertEqual(payload['equipment_name'], '礼宾枪（新）')
        self.assertEqual(payload['status'], '维修中')
        self.assertEqual(payload['total_quantity'], 5)
        self.assertEqual(payload['available_quantity'], 5)
        self.assertEqual(payload['storage_location'], '器材室 C 区')

        blocked = self.client.post('/api/club/equipment/1/borrow', headers=self.headers,
                                   json={'quantity': 1})
        self.assertEqual(blocked.status_code, 409, '非正常状态的设备不应可领用')

        self.client.put('/api/club/equipment/1', headers=self.headers, json={'status': '正常'})
        borrowed = self.client.post('/api/club/equipment/1/borrow', headers=self.headers,
                                    json={'quantity': 2})
        self.assertEqual(borrowed.status_code, 201)

        shrink = self.client.put('/api/club/equipment/1', headers=self.headers,
                                 json={'total_quantity': 1})
        self.assertEqual(shrink.status_code, 409, '总量不能低于在外占用数')
        self.assertEqual(self.client.delete('/api/club/equipment/1',
                                            headers=self.headers).status_code, 409)

        self.client.put('/api/club/equipment/usages/1/return', headers=self.headers, json={})
        still_blocked = self.client.delete('/api/club/equipment/1', headers=self.headers)
        self.assertEqual(still_blocked.status_code, 409)
        self.assertIn('报废', still_blocked.get_json()['msg'])

    def test_equipment_delete_without_history(self):
        created = self.client.post('/api/club/equipment', headers=self.headers, json={
            'asset_code': 'T-DEL-1', 'equipment_name': '待删除设备',
            'category_id': 1, 'total_quantity': 1})
        self.assertEqual(created.status_code, 201)
        new_id = created.get_json()['equipment']['equipment_id']
        self.assertEqual(self.client.delete(f'/api/club/equipment/{new_id}',
                                            headers=self.headers).status_code, 200)
        listing = self.client.get('/api/club/equipment', headers=self.headers).get_json()
        self.assertNotIn(new_id, [row['equipment_id'] for row in listing['equipment']])

    def test_duplicate_asset_code_returns_409(self):
        """资产编号是唯一约束，重复登记原本会抛 IntegrityError 变成 500。"""
        dup = self.client.post('/api/club/equipment', headers=self.headers, json={
            'asset_code': 'T-001', 'equipment_name': '重复编号', 'category_id': 1})
        self.assertEqual(dup.status_code, 409)

    def test_equipment_malformed_input_returns_4xx(self):
        cases = [
            {},
            {'asset_code': 'X-1', 'equipment_name': '缺分类'},
            {'asset_code': 'X-2', 'equipment_name': '分类非数字', 'category_id': 'abc'},
            {'asset_code': 'X-3', 'equipment_name': '数量非数字', 'category_id': 1,
             'total_quantity': 'abc'},
            {'asset_code': 'X-4', 'equipment_name': '状态非法', 'category_id': 1,
             'status': '不存在的状态'},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                response = self.client.post('/api/club/equipment', headers=self.headers,
                                            json=payload)
                self.assertIn(response.status_code, (400, 404))

    def test_equipment_management_requires_logistics_or_captain(self):
        denied = self.client.put('/api/club/equipment/1', headers=self.member_headers,
                                 json={'equipment_name': '非法改名'})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.client.delete('/api/club/equipment/1',
                                            headers=self.member_headers).status_code, 403)

    def test_scrapped_equipment_excluded_from_stock_count(self):
        before = self.client.get('/api/club/overview', headers=self.headers).get_json()
        self.assertEqual(before['equipment_count'], 2)
        self.client.put('/api/club/equipment/1', headers=self.headers, json={'status': '报废'})
        after = self.client.get('/api/club/overview', headers=self.headers).get_json()
        self.assertEqual(after['equipment_count'], 0)

    def test_social_update_and_delete(self):
        created = self.client.post('/api/club/office/socials', headers=self.headers, json={
            'platform': '微信公众号', 'account_name': '校国旗护卫队',
            'link_url': 'https://example.com'})
        self.assertEqual(created.status_code, 201)
        account_id = created.get_json()['account']['account_id']

        updated = self.client.put(f'/api/club/office/socials/{account_id}', headers=self.headers,
                                  json={'account_name': '改名后的账号'})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.get_json()['account']['account_name'], '改名后的账号')
        self.assertEqual(updated.get_json()['account']['platform'], '微信公众号',
                         '未提交的字段应保持不变')

        blank = self.client.put(f'/api/club/office/socials/{account_id}', headers=self.headers,
                                json={'platform': '   '})
        self.assertEqual(blank.status_code, 400)

        self.assertEqual(self.client.delete(f'/api/club/office/socials/{account_id}',
                                            headers=self.headers).status_code, 200)
        listing = self.client.get('/api/club/office/socials', headers=self.headers).get_json()
        self.assertEqual(listing['accounts'], [])

    def test_archive_update_and_delete_keeps_file(self):
        uploaded = self.client.post('/api/files/upload', headers=self.headers, data={
            'file': (BytesIO(b'pdf-bytes'), '制度汇编.pdf'), 'visibility': '1',
        }, content_type='multipart/form-data')
        self.assertEqual(uploaded.status_code, 201)
        file_id = uploaded.get_json()['file']['file_id']

        created = self.client.post('/api/club/office/archives', headers=self.headers, json={
            'title': '制度文件', 'archive_type': '制度文件', 'file_id': file_id})
        self.assertEqual(created.status_code, 201)
        archive_id = created.get_json()['archive']['archive_id']

        updated = self.client.put(f'/api/club/office/archives/{archive_id}', headers=self.headers,
                                  json={'title': '2026 制度汇编', 'archive_type': '其他'})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.get_json()['archive']['title'], '2026 制度汇编')

        self.assertEqual(self.client.delete(f'/api/club/office/archives/{archive_id}',
                                            headers=self.headers).status_code, 200)
        still_there = self.client.get(f'/api/files/{file_id}', headers=self.headers)
        self.assertEqual(still_there.status_code, 200, '删除档案不应连带删掉原始文件')

    def test_archive_management_requires_office_or_captain(self):
        created = self.client.post('/api/club/office/socials', headers=self.headers, json={
            'platform': '测试平台', 'account_name': '账号', 'link_url': 'https://example.com'})
        account_id = created.get_json()['account']['account_id']
        self.assertEqual(self.client.put(f'/api/club/office/socials/{account_id}',
                                         headers=self.member_headers,
                                         json={'account_name': 'x'}).status_code, 403)
        self.assertEqual(self.client.delete(f'/api/club/office/socials/{account_id}',
                                            headers=self.member_headers).status_code, 403)

    def test_terminated_activity_excluded_from_overview_and_time_center(self):
        """三处口径统一：课表、首页近期活动、时间中心都排除已终止项目。"""
        now = datetime.now()
        created = self.client.post('/api/club/activities', headers=self.headers, json={
            'title': '将被终止', 'start_time': (now + timedelta(days=1)).isoformat()})
        activity_id = created.get_json()['activity']['activity_id']

        before = self.client.get('/api/club/overview', headers=self.headers).get_json()
        self.assertEqual(before['upcoming_count'], 1)
        self.assertEqual(len(before['upcoming']), 1)

        self.client.post(f'/api/club/activities/{activity_id}/terminate',
                         headers=self.headers, json={'reason': '天气原因'})

        after = self.client.get('/api/club/overview', headers=self.headers).get_json()
        self.assertEqual(after['upcoming_count'], 0)
        self.assertEqual(after['upcoming'], [])
        self.assertEqual(after['activity_count'], 1,
                         '总数仍含已终止，与活动项目页「全部」标签保持一致')

        time_data = self.client.get('/api/club/time/overview', headers=self.headers,
                                    query_string={
                                        'start': now.isoformat(),
                                        'end': (now + timedelta(days=7)).isoformat(),
                                    }).get_json()
        self.assertEqual(time_data['activity_count'], 0)
        self.assertEqual(time_data['events'], [])


    def test_non_string_fields_do_not_500(self):
        """直接调 JSON 接口能传数字、数组、对象，一律收敛成 4xx 或按文本处理。"""
        cases = [
            ('put', '/api/club/equipment/1', {'equipment_name': 123}),
            ('put', '/api/club/equipment/1', {'notes': ['a', 'b']}),
            ('put', '/api/club/equipment/1', {'storage_location': {'a': 1}}),
            ('put', '/api/club/equipment/1', {'status': 123}),
            ('put', '/api/club/equipment/1', {'total_quantity': 'abc'}),
            ('put', '/api/club/equipment/1', {'category_id': 'abc'}),
            ('put', '/api/club/equipment/1', {'category_id': None}),
            ('post', '/api/club/equipment',
             {'asset_code': 123, 'equipment_name': 'x', 'category_id': 1}),
        ]
        for method, url, payload in cases:
            with self.subTest(payload=payload):
                sender = self.client.put if method == 'put' else self.client.post
                response = sender(url, headers=self.headers, json=payload)
                self.assertLess(response.status_code, 500)

    def test_rejected_update_does_not_poison_session(self):
        """校验失败返回 4xx 后，会话里不能留下半截修改去污染后续请求。

        改成「先算新值、全部校验通过再写回」之前，把 link_url 传成数组会让
        row.link_url 变成 None 并留在会话中，下一个需要 commit 的请求就会
        以 NOT NULL 约束冲突的形式爆掉。
        """
        social = self.client.post('/api/club/office/socials', headers=self.headers, json={
            'platform': '测试平台', 'account_name': '账号',
            'link_url': 'https://example.com'})
        account_id = social.get_json()['account']['account_id']

        rejected = self.client.put(f'/api/club/office/socials/{account_id}',
                                   headers=self.headers, json={'link_url': ['不是字符串']})
        self.assertEqual(rejected.status_code, 400)

        uploaded = self.client.post('/api/files/upload', headers=self.headers, data={
            'file': (BytesIO(b'pdf-bytes'), '后续上传.pdf'), 'visibility': '1',
        }, content_type='multipart/form-data')
        self.assertEqual(uploaded.status_code, 201, '被拒的修改不应影响后续写库请求')

        listing = self.client.get('/api/club/office/socials', headers=self.headers).get_json()
        self.assertEqual(listing['accounts'][0]['link_url'], 'https://example.com',
                         '被拒的修改不应落库')


if __name__ == '__main__':
    unittest.main()
