"""Local administrator UI. Untrusted page/checkpoint strings are text only."""
HTML = r'''<!doctype html><html lang="vi"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Điều khiển UIA / DOM</title>
<style>body{font:16px system-ui;background:#101725;color:#e6edf7;max-width:1000px;margin:auto;padding:2rem}button,select,input,textarea{font:inherit;background:#253653;color:inherit;border:1px solid #526681;padding:.5rem;margin:.3rem;border-radius:6px}article{padding:1rem;border:1px solid #40516d;margin:1rem 0;border-radius:10px}pre{white-space:pre-wrap;overflow-wrap:anywhere}textarea{display:block;width:95%;height:6rem}label{display:block;margin-top:.8rem}small{display:block;color:#b9c8e1}button:disabled{opacity:.55}</style>
<h1>Điều khiển UIA / DOM</h1><p>Chọn phiên trước khi xem hoặc điều khiển. Hủy không hoàn tác thao tác đã gửi; khôi phục có thể đóng tab do Actuator quản lý.</p>
<label for="session">Phiên đang hoạt động</label><select id="session"></select><button id="refresh">Làm mới</button><p id="message" role="status"></p>
<h2>Trình duyệt</h2><p>Chọn tab ở trình duyệt đã bật CDP. Giao diện không tự bật debugging, đăng nhập hoặc sao chép cookie.</p>
<button id="default-session">Chọn phiên MCP hiện tại</button><label for="endpoints">Địa chỉ CDP cục bộ (tùy chọn, tối đa 8 dòng)</label><textarea id="endpoints" placeholder="Để trống để kiểm tra cổng 9222 và 9223"></textarea><button id="browser-refresh">Tìm tab trình duyệt</button><button id="browser-detach">Ngắt kết nối DOM của phiên</button><main id="browsers"></main><main id="bookmarks"></main>
<h2>Lệnh DOM</h2><main id="operations"></main><h2>Tiến độ kiểm tra link</h2><main id="reviews"></main><div id="checkpoint"></div>
<label for="task">Tên tác vụ</label><input id="task" placeholder="Tên tác vụ"><label for="urls">Link cần kiểm tra, mỗi dòng một URL</label><textarea id="urls" placeholder="Mỗi dòng một URL cần kiểm tra"></textarea>
<label for="details">Selector vùng chi tiết (tùy chọn)</label><input id="details" placeholder="Selector vùng chi tiết (tùy chọn)"><label for="variants">Selector phân loại (tùy chọn)</label><input id="variants" placeholder="Selector phân loại (tùy chọn)"><button id="resume">Tiếp tục tối đa 3 link</button>
<details><summary>Chi tiết kết quả</summary><pre id="result"></pre></details>
<script>
const headers={'X-Control-Center-Token':__TOKEN__,'Content-Type':'application/json'};
const $=id=>document.getElementById(id), sid=()=>$('session').value;
const names={received:'Đã nhận',queued:'Đang chờ',running:'Đang chạy',cancelling:'Đang hủy',cancelled:'Đã hủy',timed_out:'Hết thời gian',failed:'Thất bại',completed:'Hoàn tất',blocked:'Bị chặn',unknown:'Chưa rõ kết quả',pending:'Chờ đọc lại',title:'Tiêu đề',detail:'Chi tiết',variants:'Phân loại',action:'Thao tác'};
let generation=0,reviewGeneration=0;
let browserGeneration=0;
async function api(path,body){
 const response=await fetch(path,{headers,method:body?'POST':'GET',body:body?JSON.stringify(body):undefined});
 const data=await response.json();if(!response.ok)throw Error(data.error||'Yêu cầu thất bại');return data;
}
async function guarded(fn){try{$('message').textContent='Đang xử lý…';await fn();$('message').textContent='Đã cập nhật'}catch(e){$('message').textContent=e.message}}
function show(data){$('result').textContent=JSON.stringify(data,null,2)}
async function browserAction(action,selection,id){
 if(id!==sid())throw Error('Phiên đã thay đổi. Đọc lại danh sách trình duyệt.');
 const result=await api('/api/automation/browser',{session_id:id,action,selection});if(id===sid())show(result);
}
async function loadBrowsers(){
 const id=sid(),request=++browserGeneration;if(!id)throw Error('Chọn phiên trước khi tìm tab.');
 const endpoints=$('endpoints').value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);
 const query='?session='+encodeURIComponent(id)+endpoints.map(x=>'&endpoint='+encodeURIComponent(x)).join('');
 const data=await api('/api/automation/browsers'+query);if(id!==sid()||request!==browserGeneration)return;
 $('browsers').replaceChildren();$('bookmarks').replaceChildren();
 for(const browser of data.sessions){
  if(!browser.tabs.length){const note=document.createElement('p');note.textContent=browser.cdp_url+' — chưa có tab HTTP/HTTPS sẵn sàng';$('browsers').append(note)}
  for(const tab of browser.tabs){
   const card=document.createElement('article'),title=document.createElement('strong'),url=document.createElement('small');title.textContent=tab.title||'Tab trình duyệt';url.textContent=tab.url;card.append(title,url);
   const selection={cdp_url:browser.cdp_url,tab_id:tab.tab_id,expected_url:tab.url};
   const attach=document.createElement('button');attach.textContent='Kết nối tab này';attach.onclick=()=>guarded(async()=>{attach.disabled=true;try{await browserAction('attach',selection,id)}finally{attach.disabled=false}});card.append(attach);
   const name=document.createElement('input');name.placeholder='Tên tab để nhớ';name.setAttribute('aria-label','Tên tab để nhớ');
   const remember=document.createElement('button');remember.textContent='Ghi nhớ lựa chọn';remember.onclick=()=>guarded(async()=>{remember.disabled=true;try{await browserAction('remember',{...selection,name:name.value,expected_revision:0},id);await loadBrowsers()}finally{remember.disabled=false}});card.append(name,remember);$('browsers').append(card);
  }
 }
 const choices=await api('/api/automation/browser-choices?session='+encodeURIComponent(id));if(id!==sid()||request!==browserGeneration)return;
 for(const choice of choices.choices){const button=document.createElement('button');button.textContent='Kết nối lại '+choice.name;button.onclick=()=>guarded(async()=>{button.disabled=true;try{await browserAction('resume',{name:choice.name},id)}finally{button.disabled=false}});$('bookmarks').append(button)}
}
$('default-session').onclick=()=>guarded(async()=>{
 const result=await api('/api/automation/browser-default?session='+encodeURIComponent(sid()));
 if(!result.default_session_id)throw Error('Chưa có phiên MCP hiện tại. Gọi một tool đọc qua kết nối Actuator rồi làm mới.');
 if(![...$('session').options].some(x=>x.value===result.default_session_id)){const option=document.createElement('option');option.value=result.default_session_id;option.textContent='Phiên MCP hiện tại — '+result.owner;$('session').append(option)}
 $('session').value=result.default_session_id;await load();
});
$('browser-refresh').onclick=()=>guarded(loadBrowsers);
$('browser-detach').onclick=()=>guarded(async()=>{if(!confirm('Các tab DOM do phiên tạo ra có thể đóng. Nếu đang dùng tab CDP đã chọn, tab gốc được giữ. Tiếp tục?'))return;await browserAction('detach',{acknowledge_close:true},sid());await load()});
async function showReview(task,id){
 const request=++reviewGeneration;
 const data=await api('/api/automation/reviews?session='+encodeURIComponent(id)+'&task='+encodeURIComponent(task));
 if(id!==sid()||request!==reviewGeneration)return;
 $('task').value=task;show(data);$('urls').value=[...new Set(data.items.map(x=>x.url))].join('\n');
 $('details').value=data.plan?.detail_selector||'';$('variants').value=data.plan?.variant_selector||'';
 const root=$('checkpoint');root.replaceChildren();
 if(!data.plan){const warning=document.createElement('p');warning.textContent='Checkpoint cũ chưa lưu selector. Dùng tên tác vụ mới và chọn vùng cần kiểm tra lại.';root.append(warning)}
 if(data.truncated){const warning=document.createElement('p');warning.textContent='Danh sách đang bị giới hạn. Dùng tác vụ nhỏ hơn; chưa thể tiếp tục từ danh sách này.';root.append(warning)}
 for(const item of data.items){
  const card=document.createElement('article'),title=document.createElement('strong'),url=document.createElement('small');
  title.textContent=(names[item.stage]||item.stage)+' — '+(names[item.status]||item.status);url.textContent=item.url;card.append(title,url);
  if(data.plan&&!data.truncated&&['title','detail','variants'].includes(item.stage)&&['blocked','unknown'].includes(item.status)){
   const reset=document.createElement('button');reset.textContent='Đã kiểm tra, cho phép đọc lại';
   reset.onclick=()=>guarded(async()=>{
    if(id!==sid())throw Error('Phiên đã thay đổi. Đọc lại tiến độ trước khi xác nhận.');
    if(!confirm('Bạn đã kiểm tra kết quả trước và muốn đặt vùng này về chờ đọc lại? Bấm Tiếp tục riêng để mở tab mới và đọc.'))return;
    reset.disabled=true;
    try{const result=await api('/api/automation/reset-checkpoint',{session_id:id,task_key:task,item_key:item.item_key,expected_revision:item.revision,acknowledge_inspected:true});if(id===sid()&&$('task').value===task){show(result);await showReview(task,id)}}finally{reset.disabled=false}
   });card.append(reset);
  }root.append(card);
 }
}
async function load(){
 const id=sid(),request=++generation;++reviewGeneration;++browserGeneration;$('operations').replaceChildren();$('reviews').replaceChildren();$('checkpoint').replaceChildren();$('browsers').replaceChildren();$('bookmarks').replaceChildren();
 if(!id)return;
 const data=await api('/api/automation/operations?session='+encodeURIComponent(id));if(id!==sid()||request!==generation)return;
 for(const operation of data.operations){
  const card=document.createElement('article'),text=document.createElement('p');text.textContent=operation.method+' — '+(names[operation.state]||operation.state)+' — '+Math.round(operation.elapsed_seconds||0)+' giây';card.append(text);
  for(const action of ['cancel','recover']){
   if(action==='recover'?operation.state!=='cancelling':!['queued','running'].includes(operation.state))continue;
   const button=document.createElement('button');button.textContent=action==='cancel'?'Hủy lệnh':'Khôi phục driver';
   button.onclick=()=>guarded(async()=>{
    if(id!==sid())throw Error('Phiên đã thay đổi. Làm mới trước khi điều khiển.');
    if(action==='recover'&&!confirm('Có thể đóng tab DOM do Actuator quản lý và mất nội dung chưa lưu. Tiếp tục?'))return;
    button.disabled=true;
    try{show(await api('/api/automation/control',{session_id:id,action,operation_id:operation.operation_id,acknowledge_close:action==='recover'}));await load()}finally{button.disabled=false}
   });card.append(button);
  }$('operations').append(card);
 }
 const review=await api('/api/automation/reviews?session='+encodeURIComponent(id));if(id!==sid()||request!==generation)return;
 for(const task of review.tasks){const button=document.createElement('button');button.textContent=task.task_key+' — '+Object.entries(task.counts).map(([state,count])=>(names[state]||state)+': '+count).join(', ');button.onclick=()=>guarded(()=>showReview(task.task_key,id));$('reviews').append(button)}
 if(review.truncated){const warning=document.createElement('p');warning.textContent='Chỉ hiển thị một phần danh sách tác vụ. Nhập tên tác vụ để đọc tiến độ đầy đủ.';$('reviews').append(warning)}
}
$('refresh').onclick=()=>guarded(load);$('session').onchange=()=>guarded(load);
$('resume').onclick=()=>guarded(async()=>{
 const button=$('resume'),id=sid();button.disabled=true;
 try{const review={task_key:$('task').value,urls:$('urls').value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean)};
  if($('details').value)review.detail_selector=$('details').value;if($('variants').value)review.variant_selector=$('variants').value;
  const result=await api('/api/automation/resume',{session_id:id,review});if(id===sid()){show(result);await load()}
 }finally{button.disabled=false}
});
guarded(async()=>{const data=await api('/api/automation/sessions');for(const session of data.sessions){const option=document.createElement('option');option.value=session.session_id;option.textContent=session.session_id+' — '+session.owner;$('session').append(option)}await load()});
</script></html>'''
