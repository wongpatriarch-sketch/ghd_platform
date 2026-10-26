const BASE='/api';
function getToken(){return localStorage.getItem('token')||''}
async function request(method,path,data=null){
  const headers={}; if(getToken())headers.Authorization='Bearer '+getToken();
  const opts={method,headers}; if(data!==null){if(data instanceof FormData){opts.body=data}else{headers['Content-Type']='application/json';opts.body=JSON.stringify(data)}}
  try{
    const res=await fetch(BASE+path,opts);
    const body=await res.json().catch(()=>({}));
    // 只有后端明确回「请先登录」才当作令牌失效。登录时密码错误、改密码时旧密码错误
    // 同样返回 401，但那些情况不该把人踢下线，所以按 msg 精确判断而不是按状态码。
    if(res.status===401&&body.msg==='请先登录'&&getToken()&&typeof handleSessionExpired==='function')handleSessionExpired();
    return{status:res.status,data:body};
  }
  catch(error){return{status:0,data:{msg:'无法连接服务器，请确认后端已启动'},error}}
}
const API={
  login:d=>request('POST','/auth/login',d),register:d=>request('POST','/auth/register',d),profile:()=>request('GET','/auth/profile'),updateProfile:d=>request('PUT','/auth/profile',d),
  overview:()=>request('GET','/club/overview'),departments:()=>request('GET','/club/departments'),members:()=>request('GET','/club/members'),
  timeOverview:params=>request('GET','/club/time/overview?'+new URLSearchParams(params)),timeRecords:params=>request('GET','/club/time/records?'+new URLSearchParams(params)),
  schedule:params=>request('GET','/club/schedule?'+new URLSearchParams(params)),myPoints:()=>request('GET','/club/points/my'),
  activities:(status='')=>request('GET','/club/activities'+(status?'?status='+encodeURIComponent(status):'')),createActivity:d=>request('POST','/club/activities',d),activity:id=>request('GET',`/club/activities/${id}`),activityRecord:id=>request('GET',`/club/activities/${id}/record`),editActivity:(id,d)=>request('PUT',`/club/activities/${id}`,d),terminateActivity:(id,d)=>request('POST',`/club/activities/${id}/terminate`,d),checkIn:id=>request('POST',`/club/activities/${id}/check-in`,{}),transferActivity:(id,d)=>request('PUT',`/club/activities/${id}/transfer`,d),updateParticipant:(id,d)=>request('PUT',`/club/activities/${id}/participants`,d),saveSummary:(id,d)=>request('PUT',`/club/activities/${id}/summary`,d),
  equipment:()=>request('GET','/club/equipment'),createEquipment:d=>request('POST','/club/equipment',d),equipmentCategories:()=>request('GET','/club/equipment/categories'),createEquipmentCategory:d=>request('POST','/club/equipment/categories',d),updateEquipmentCategory:(id,d)=>request('PUT',`/club/equipment/categories/${id}`,d),deleteEquipmentCategory:id=>request('DELETE',`/club/equipment/categories/${id}`),borrow:(id,d)=>request('POST',`/club/equipment/${id}/borrow`,d),usages:()=>request('GET','/club/equipment/usages?active=1'),returnEquipment:id=>request('PUT',`/club/equipment/usages/${id}/return`,{}),updateEquipment:(id,d)=>request('PUT',`/club/equipment/${id}`,d),deleteEquipment:id=>request('DELETE',`/club/equipment/${id}`),
  socials:()=>request('GET','/club/office/socials'),createSocial:d=>request('POST','/club/office/socials',d),archives:()=>request('GET','/club/office/archives'),createArchive:d=>request('POST','/club/office/archives',d),mediaQueue:()=>request('GET','/club/office/media'),reviewMedia:(id,d)=>request('PUT',`/club/office/media/${id}/review`,d),updateSocial:(id,d)=>request('PUT',`/club/office/socials/${id}`,d),deleteSocial:id=>request('DELETE',`/club/office/socials/${id}`),updateArchive:(id,d)=>request('PUT',`/club/office/archives/${id}`,d),deleteArchive:id=>request('DELETE',`/club/office/archives/${id}`)
  ,governance:()=>request('GET','/governance/dashboard'),reviewMembership:(id,d)=>request('PUT',`/governance/membership-requests/${id}/review`,d),createAccount:d=>request('POST','/governance/accounts',d),deleteAccount:(id,d)=>request('DELETE',`/governance/accounts/${id}`,d),requestDeactivation:d=>request('POST','/governance/deactivation-request',d),applyDepartment:d=>request('POST','/governance/department-applications',d),reviewDepartment:(id,d)=>request('PUT',`/governance/department-applications/${id}/review`,d),createGrant:d=>request('POST','/governance/grants',d),revokeGrant:id=>request('DELETE',`/governance/grants/${id}`),proposeTransfer:d=>request('POST','/governance/transfers',d),respondTransfer:(id,d)=>request('PUT',`/governance/transfers/${id}/respond`,d),forumPosts:()=>request('GET','/governance/forum/posts'),createPost:d=>request('POST','/governance/forum/posts',d),createComment:(id,d)=>request('POST',`/governance/forum/posts/${id}/comments`,d),deletePost:id=>request('DELETE',`/governance/forum/posts/${id}`)
  ,myFiles:params=>request('GET','/files/my?'+new URLSearchParams({per_page:100,...(params||{})})),uploadFile:d=>request('POST','/files/upload',d),fileUrl:(id,action)=>`/api/files/${id}/${action}?token=${encodeURIComponent(getToken())}`,submitMedia:(id,d)=>request('POST',`/club/activities/${id}/media`,d)
  ,changePassword:d=>request('PUT','/auth/password',d)
};
