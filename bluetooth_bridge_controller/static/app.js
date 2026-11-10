const $=selector=>document.querySelector(selector);
const VM=BridgeViewModel;
let state=null,connected=false,refreshing=false,selectedTarget='',renewTimer=null,renewingSession='',actionBusy=false,lastUpdated=0,detailDesktop='';
let toastTimer=null;
function escapeHtml(value){return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function pill(text,tone='neutral'){return `<span class="pill ${tone}">${escapeHtml(text)}</span>`;}
function html(element,markup){
  if(element.innerHTML===markup)return;
  const focus=document.activeElement,keys=['desktop','details','use','wake','reconcile','retry','ack'];
  const key=element.contains(focus)?keys.find(k=>focus.dataset?.[k]!==undefined):null;
  const value=key?focus.dataset[key]:null,top=element.scrollTop,left=element.scrollLeft;
  element.innerHTML=markup;element.scrollTop=top;element.scrollLeft=left;
  if(key){const replacement=[...element.querySelectorAll(`[data-${key}]`)].find(node=>node.dataset[key]===value);if(replacement&&!replacement.disabled)replacement.focus({preventScroll:true});}
}
function fmtTime(timestamp){return timestamp?new Date(timestamp*1000).toLocaleString('zh-CN',{hour12:false}):'尚无记录';}
function clockTime(timestamp){return new Date(timestamp*1000).toLocaleTimeString('zh-CN',{hour12:false});}
function toast(message){const el=$('#toast');el.textContent=message;el.classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.classList.remove('show'),5000);}
async function api(path,method='GET',body){
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),method==='GET'?10000:40000);
  try{const response=await fetch(path,{method,headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined,signal:controller.signal,cache:'no-store'});let value;try{value=await response.json();}catch{throw new Error('后台返回内容无法读取，请刷新状态');}if(!response.ok||value.ok===false)throw new Error(value.error||`请求未完成（${response.status}）`);return value;}
  catch(error){if(error.name==='AbortError')throw new Error('请求超时，请先刷新并核对结果');if(error instanceof TypeError)throw new Error('无法连接管理后台，请检查 Mac 和 Tailscale');throw error;}finally{clearTimeout(timer);}
}
function agentById(id){return state?.agents.find(a=>a.id===Number(id));}
function cloudFor(agent){return state?.management?.inventory.find(d=>d.desktop_id===agent?.desktop_id)?.cloud_status||'Unknown';}
function setView(view){if(!['daily','devices','tasks','help'].includes(view))view='daily';document.querySelectorAll('[data-panel]').forEach(p=>p.hidden=p.dataset.panel!==view);document.querySelectorAll('nav [data-view]').forEach(link=>{if(link.dataset.view===view)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');});}
window.addEventListener('hashchange',()=>setView(location.hash.slice(1)));setView(location.hash.slice(1));
function chooseTarget(id){selectedTarget=String(id);$('#targetSearch').value='';location.hash='daily';setView('daily');render();}
function showDevice(desktop){detailDesktop=desktop;renderDeviceDetails();$('#deviceDialog').showModal();}
function renderDeviceDetails(){
  if(!state||!detailDesktop)return;
  const row=VM.records(state).find(d=>d.desktop_id===detailDesktop);if(!row)return;
  const a=row.agent,r=a?.last_report||{},status=VM.device(a,row.cloud_status,state.lease);
  $('#deviceDialogTitle').textContent=row.display_name;
  const items=[['云电脑状态',VM.cloudNames[row.cloud_status]||'状态待同步'],['接入状态',a?.machine_id?'已接入':'待接入'],['代理连接',connected?(a?.online?'在线':'离线'):'状态未确认'],['最近心跳',fmtTime(a?.last_seen)],['USB 状态',a?.online?({released:'已释放',attached:'已连接',preparing:'准备中'}[r.vh_status]||'待确认'):'等待新心跳'],['蓝牙状态',a?.online?({ready:'Windows 蓝牙就绪',absent:'适配器未分配',preparing:'正在识别'}[r.bluetooth_status]||'待确认'):'等待新心跳'],['电脑 ID',row.desktop_id],['主机名',a?.hostname||'尚未接入'],['Tailscale 地址',a?.tailscale_ip||'尚未分配'],['最近错误',r.error||'未回报错误']];
  html($('#deviceDetails'),pill(connected?status.text:'当前状态未确认',connected?status.tone:'neutral')+`<p class="hint" style="margin-top:10px">${escapeHtml(status.description)} 离线时的错误是最后一次回报。</p><dl class="detail-list">${items.map(([name,value])=>`<div><dt>${escapeHtml(name)}</dt><dd class="${name==='最近错误'&&r.error?'error':''}">${escapeHtml(value)}</dd></div>`).join('')}</dl>`);
}
function render(){
  if(!state)return;
  const s=state,m=s.management||{},l=s.lease,current=agentById(l.target_id),pending=agentById(l.pending_target_id),source=VM.source(s,connected);
  const freshAgent=current?.online,currentReport=current?.last_report||{};
  const sourceOkay=source.adapter.tone==='good'&&source.bridge.tone==='good'&&(!m.source_reset_configured||m.source_reset_ready)&&!l.reset_error;
  let title='未共享蓝牙',description='当前没有电脑占用，可以选择电脑开始使用。',tone='neutral';
  if(l.state==='resetting'){title='正在准备蓝牙';description=`正在为 ${pending?.display_name||'所选电脑'} 准备通道，完成后自动连接。`;tone='warn';}
  else if(l.state==='releasing'){title='正在释放蓝牙';description=pending?`等待 ${current?.display_name||'原电脑'} 释放，再连接 ${pending.display_name}。`:'等待原电脑确认释放，期间请保留桌面连接。';tone='warn';}
  else if(l.state==='authorizing'){title='正在连接蓝牙';description=`${current?.display_name||'所选电脑'} 正在识别适配器，请稍候。`;tone='warn';}
  else if(l.state==='active'){const d=VM.device(current,cloudFor(current),l);title=d.tone==='good'?`正在使用 · ${current?.display_name||'云电脑'}`:'共享状态待确认';description=d.description;tone=d.tone;}
  if(connected&&l.state==='active'&&!sourceOkay){title='共享通道需要检查';description='源端尚未确认就绪，当前共享状态待确认。请查看下方连接状态。';tone='warn';}
  if(l.reset_error){title='蓝牙准备失败';description='本次连接已停止，请到问题处理查看具体原因。';tone='bad';}
  else if(connected&&l.state==='idle'&&source.adapter.tone==='bad'){title='USB 蓝牙需要检查';description=source.adapter.text+'。检查适配器后，再开始使用。';tone='bad';}
  else if(connected&&l.state==='idle'&&source.bridge.tone==='bad'){title='桥接通道未就绪';description='请查看连接状态和问题处理，通道恢复后才能开始使用。';tone='bad';}
  if(!connected){title='管理后台连接中断';description='下方为上次获取的数据，当前蓝牙连接状态未确认。';tone='bad';}
  $('#currentTarget').textContent=title;$('#leaseDetail').textContent=description;$('#statusBanner').className='status-banner '+tone;
  $('#systemPill').className='pill '+(connected?(l.state==='idle'&&sourceOkay?'good':tone):'bad');
  $('#systemPill').textContent=connected?(l.state==='idle'?(sourceOkay?'源端已就绪':'源端待检查'):l.state==='active'&&tone==='good'?'蓝牙共享中':title):'后台连接中断';
  html($('#adapterStatus'),pill(source.adapter.text,source.adapter.tone));html($('#bridgeStatus'),pill(source.bridge.text,source.bridge.tone));
  $('#currentComputer').textContent=connected?(current?.display_name||(l.state==='resetting'?pending?.display_name+'（准备中）':'无电脑占用')):'状态未确认';
  $('#remoteBluetooth').textContent=!connected?'状态未确认':!current?'等待分配':!freshAgent?'代理离线，状态待确认':currentReport.error?'代理回报异常，状态待确认':l.state==='releasing'?'等待释放确认':currentReport.bluetooth_status==='ready'&&currentReport.vh_status==='attached'?'适配器已识别 · 蓝牙就绪':currentReport.vh_status==='preparing'?'正在识别适配器':currentReport.bluetooth_status==='absent'?'等待适配器连接':'等待状态确认';
  html($('#resetStatus'),pill(!connected?'状态未确认':m.source_reset_ready?'已就绪':m.source_reset_configured?'组件待检查':'未配置',connected&&m.source_reset_ready?'good':connected?'warn':'neutral'));
  $('#lastUpdated').textContent=lastUpdated?`最近更新 ${clockTime(lastUpdated)}`:'等待首次更新';$('#statusChecked').textContent=connected?`页面每 3 秒更新 · USB 检测 ${VM.relative(m.source_hardware?.checked_at,s.now)}`:`上次成功更新 ${clockTime(lastUpdated)}`;
  $('.status-footer').classList.toggle('offline',!connected);
  const records=VM.records(s),registered=s.agents.filter(a=>a.machine_id),issues=(m.incidents||[]).length+(m.failed_notifications?1:0);
  $('#totalDevices').textContent=records.length;$('#registeredDevices').textContent=registered.length;$('#onlineDevices').textContent=connected?registered.filter(a=>a.online).length:'—';$('#issueCount').textContent=connected?issues:'—';$('#issueCount').classList.toggle('has-issues',issues>0);$('#issueCaption').textContent=connected?(issues?'查看问题处理':'暂无待处理提醒'):'等待后台恢复';
  renderTargetSelector(registered);
  const a=agentById(selectedTarget),d=VM.device(a,cloudFor(a),l),busy=['resetting','authorizing','releasing'].includes(l.state);
  html($('#selectedDeviceStatus'),a?pill(connected?d.text:'状态未确认',connected?d.tone:'neutral')+`<p>${escapeHtml(connected?d.description:'恢复后台连接后，才能继续操作。')}</p>`:'<p>暂无匹配电脑，可清空搜索或到电脑管理接入。</p>');
  const start=$('#startButton');start.textContent=actionBusy?'正在提交…':!a?'选择电脑后开始':!a.online?'唤醒并检查 '+a.display_name:l.state==='active'&&l.target_id===a.id?'正在使用 '+a.display_name:current&&current.id!==a.id?'切换到 '+a.display_name:'开始使用 '+a.display_name;
  const validOnline=!!a?.online&&d.tone!=='bad'&&d.text==='在线待用';
  start.disabled=!connected||actionBusy||busy||!a||(!a.online&&!m.cloud_configured)||(a.online&&(!validOnline||source.adapter.tone!=='good'||source.bridge.tone!=='good'||(m.source_reset_configured&&!m.source_reset_ready)));
  const release=$('#releaseButton');release.textContent=l.state==='resetting'?'取消连接':l.state==='releasing'&&pending?'取消切换':'结束使用';release.disabled=!connected||actionBusy||(l.state!=='resetting'&&(!current||(l.state==='releasing'&&!pending)));
  $('#useHint').textContent=busy?'正在完成连接或释放，请保持桌面连接。':a&&!a.online?'唤醒后请保持无影桌面连接；代理在线后，再点击开始使用。':l.state==='active'?'切换到其他电脑前，会先等待当前电脑释放。':'开始使用前自动准备蓝牙；请保持 Mac 唤醒并连接好适配器。';
  $('#leaseRemaining').hidden=!l.expires_at;$('#leaseSeconds').textContent=l.expires_at?Math.max(0,l.expires_at-s.now):'—';
  const path=[['Mac USB 适配器',source.adapter,`设备标识 ${s.config.usb}`],['私有桥接通道',source.bridge,connected?`已授权数据连接 ${m.usb_active_tunnels||0} 条`:'等待管理后台恢复'],['云电脑蓝牙',!connected?{tone:'neutral',text:'状态未确认'}:current?VM.device(current,cloudFor(current),l):{tone:l.state==='resetting'?'warn':'neutral',text:l.state==='resetting'?'准备后连接':'尚未分配'},current?.display_name||pending?.display_name||'选择电脑后开始连接'],['手机通行密钥',{tone:'neutral',text:'由手机与网站确认'},'手机放在适配器附近，按验证提示完成操作']];
  html($('#connectionPath'),path.map(([name,status,note],i)=>`<li class="connection-step"><span class="step-index">0${i+1}</span><h3>${name}</h3>${pill(status.text,status.tone)}<p>${escapeHtml(note)}</p></li>`).join(''));
  const events={lease_active:'蓝牙已连接',lease_released:'蓝牙已释放',lease_acquire_requested:'请求连接蓝牙',lease_switch_requested:'请求切换电脑',release_requested:'请求释放蓝牙',source_reset_requested:'准备蓝牙通道',source_reset_completed:'蓝牙通道准备完成',source_reset_failed:'蓝牙通道准备失败',fault_release_requested:'故障保护：请求释放',controller_restart_release:'后台恢复后释放旧连接',enrollment_created:'创建电脑接入任务',agent_enrolled:'电脑已接入',phone_passkey_verified:'用户确认手机通行密钥成功'};
  html($('#events'),s.events.length?s.events.slice(0,80).map(e=>`<div class="event"><time>${escapeHtml(fmtTime(e.created_at))}</time><strong>${escapeHtml(events[e.event]||'设备状态更新')}${e.agent_id?' · '+escapeHtml(agentById(e.agent_id)?.display_name||'已移除电脑'):''}</strong><small>${e.level==='warning'?'提醒':e.level==='error'?'异常':'记录'}</small>${!events[e.event]?`<details><summary>技术信息</summary>${escapeHtml(e.event)}</details>`:''}</div>`).join(''):'<p class="empty-state">暂无事件记录。</p>');
  if($('#deviceDialog').open)renderDeviceDetails();configureRenew();
}
function renderTargetSelector(agents){
  const query=$('#targetSearch').value.trim().toLocaleLowerCase();
  const filtered=agents.filter(a=>[a.display_name,a.desktop_id,a.hostname].join(' ').toLocaleLowerCase().includes(query));
  if(!filtered.some(a=>String(a.id)===selectedTarget))selectedTarget=String(filtered.find(a=>a.id===state.lease.target_id||a.id===state.lease.pending_target_id)?.id||filtered[0]?.id||'');
  $('#targetCount').textContent=`${filtered.length} 台匹配 / ${agents.length} 台已接入`;
  const options=filtered.length?filtered.map(a=>`<option value="${a.id}">${escapeHtml(a.display_name)} · ${connected?VM.device(a,cloudFor(a),state.lease).text:'状态未确认'}</option>`).join(''):'<option value="">没有匹配的已接入电脑</option>';
  html($('#targetSelect'),options);$('#targetSelect').value=selectedTarget;
}
async function refresh(){if(refreshing)return;refreshing=true;try{const next=await api('/api/status');if(!next.lease||!Array.isArray(next.agents)||!next.management)throw new Error('后台状态格式未确认');state=next;connected=true;lastUpdated=Math.floor(Date.now()/1000);$('#connectionBanner').hidden=true;}catch(error){connected=false;$('#connectionBanner').textContent=error.message+'。正在自动重试，当前状态未确认。';$('#connectionBanner').hidden=false;}finally{refreshing=false;if(state){render();window.renderManagement?.(state);}else{$('#systemPill').textContent='后台连接中断';$('#systemPill').className='pill bad';}}}
function configureRenew(){const lease=state?.lease,key=connected&&lease?.state==='active'?`${lease.session_id}:${lease.generation}`:'';if(key===renewingSession)return;clearInterval(renewTimer);renewingSession=key;if(key){const body={agent_id:lease.target_id,session_id:lease.session_id,generation:lease.generation};renewTimer=setInterval(()=>api('/api/lease/renew','POST',body).catch(error=>toast(error.message)),10000);}}
async function acquire(id){if(actionBusy||!connected)return;actionBusy=true;render();try{await api('/api/lease/acquire','POST',{agent_id:Number(id)});toast('连接请求已提交，正在准备蓝牙');}catch(error){toast(error.message+'；请刷新确认状态，勿重复提交。');}finally{actionBusy=false;await refresh();}}
$('#targetSearch').oninput=()=>render();$('#targetSelect').onchange=e=>{selectedTarget=e.target.value;render();};
$('#checkStatus').onclick=async()=>{const b=$('#checkStatus');b.disabled=true;await refresh();b.disabled=false;};
$('#startButton').onclick=async()=>{const a=agentById(selectedTarget);if(!a||!connected||actionBusy)return;if(!a.online){await window.bridgeWake?.(a.desktop_id);return;}await acquire(a.id);};
$('#releaseButton').onclick=async()=>{if(actionBusy||!connected)return;actionBusy=true;render();try{await api('/api/lease/release','POST',{});toast('已请求释放，正在等待电脑确认');}catch(error){toast(error.message+'；请刷新核对释放结果。');}finally{actionBusy=false;await refresh();}};
$('#closeDeviceDialog').onclick=()=>$('#deviceDialog').close();
$('#openEnroll').onclick=()=>{$('#enrollResult').hidden=true;$('#enrollResult').textContent='';$('#copyEnrollment').hidden=true;$('#enrollDialog').showModal();};
$('#enrollForm').onsubmit=async event=>{event.preventDefault();const b=event.submitter;b.disabled=true;try{const result=await api('/api/onboarding/package','POST',{display_name:$('#displayName').value,desktop_id:$('#desktopId').value});$('#enrollResult').textContent=result.command;$('#enrollResult').hidden=false;$('#copyEnrollment').hidden=false;}catch(error){toast(error.message);}finally{b.disabled=false;}};
function clearEnrollment(){$('#enrollResult').textContent='';$('#enrollResult').hidden=true;$('#enrollForm').reset();}
document.querySelectorAll('[data-close-enroll]').forEach(b=>b.onclick=()=>$('#enrollDialog').close());$('#enrollDialog').addEventListener('close',clearEnrollment);
$('#copyEnrollment').onclick=async()=>{try{await navigator.clipboard.writeText($('#enrollResult').textContent);toast('接入命令已复制，请在目标电脑管理员 PowerShell 执行');}catch{toast('复制失败，请手动选择并复制命令');}};
setInterval(refresh,3000);refresh();
