(() => {
  const selected=new Set(),activeStates=new Set(['queued','waking','submitting','preflight','running','waiting_heartbeat']);
  let pairingCode='',submission=null,submitting=false,inventoryPage=1,jobPage=1;
  const pageSize=20,jobPageSize=12,el=id=>document.getElementById(id);
  const states={queued:'排队中',waking:'等待启动 / 唤醒',submitting:'正在下发',preflight:'环境检查',running:'正在执行',waiting_heartbeat:'等待新版代理上线',succeeded:'已完成',failed:'失败',unknown:'结果待核对',skipped:'已接入，跳过安装'};
  const operations={install:'首次接入',diagnose:'读取诊断',repair:'更新 / 修复代理'};
  const stages={preparing:'准备中',cloudassistant_wait:'等待云助手就绪',preflight:'环境检查',install:'安装',repair:'更新 / 修复',command_finished:'命令完成，确认心跳',heartbeat_confirmed:'已确认新版代理在线',diagnostic_complete:'诊断已回读',already_registered:'保留原有身份'};
  const helpers={TransportUnknown:'请核对原任务结果，不要重复安装',ControllerRestartDuringSubmission:'后台重启前未取得回执，请核对原任务',ExistingIdentityRequiresReview:'已有配置，先读取诊断',RepairIdentityMismatch:'电脑身份或控制器地址不符，需核对登记',UnsupportedWindowsEnvironment:'核对 Windows、架构与 PowerShell',AgentTaskMissing:'使用更新 / 修复恢复启动项',DiagnosticOutputMissing:'到阿里云执行详情核对输出',JobDeadlineExceeded:'检查电脑启动、云助手和网络后再试',SourceValidationRequired:'完成源端检查和接管释放复测后，再接入新电脑',Configured:'已有配置，诊断完成',Unconfigured:'未接入，可以首次接入'};
  function limit(){return Math.max(1,Math.min(50,Number(state?.management?.max_batch_size)||3));}
  function redraw(){if(state)window.renderManagement(state);}
  function rows(){return state?VM.records(state):[];}
  function searchMatch(row,query){return [row.display_name,row.desktop_id,row.agent?.hostname].join(' ').toLocaleLowerCase().includes(query);}
  function selectedLabel(){
    el('selectionCount').textContent=`已选 ${selected.size} 台 / 每批最多 ${limit()} 台`;
    el('clearSelection').disabled=!selected.size||submitting;
    const byId=new Map(rows().map(row=>[row.desktop_id,row]));
    html(el('selectedNames'),[...selected].map(id=>`<span class="selection-chip">${escapeHtml(byId.get(id)?.display_name||id)}</span>`).join(''));
    const blocked=el('batchOperation').value==='install'&&!state?.management?.bulk_ready;
    el('runBatch').disabled=!connected||submitting||selected.size===0||selected.size>limit()||blocked;
    el('runBatch').textContent=submitting?'正在提交…':'执行已选任务';
  }
  function renderInventory(s){
    const records=rows(),m=s.management,query=el('inventorySearch').value.trim().toLocaleLowerCase(),filter=el('inventoryFilter').value;
    const ids=new Set(records.map(d=>d.desktop_id));for(const id of selected)if(!ids.has(id))selected.delete(id);
    const filtered=records.filter(d=>{
      if(!searchMatch(d,query))return false;
      const registered=!!d.agent?.machine_id,status=VM.device(d.agent,d.cloud_status,s.lease);
      return filter==='all'||filter==='registered'&&registered||filter==='unregistered'&&!registered||filter==='online'&&d.agent?.online||filter==='attention'&&registered&&['bad','warn'].includes(status.tone)&&!['正在连接蓝牙','正在准备蓝牙','正在释放'].includes(status.text)||filter==='sleeping'&&VM.sleeping(d.cloud_status);
    });
    const pages=Math.max(1,Math.ceil(filtered.length/pageSize));inventoryPage=Math.min(pages,inventoryPage);
    const visible=filtered.slice((inventoryPage-1)*pageSize,inventoryPage*pageSize);
    el('inventorySummary').textContent=`${filtered.length} / ${records.length} 台`;
    const markup=visible.map(d=>{
      const a=d.agent,registered=!!a?.machine_id,status=VM.device(a,d.cloud_status,s.lease),busy=m.jobs.some(j=>j.desktop_id===d.desktop_id&&(activeStates.has(j.state)||j.state==='unknown'));
      const selectable=/^ecd-[a-zA-Z0-9]+$/.test(d.desktop_id),checked=selected.has(d.desktop_id);
      const controls=`<button class="text-button" data-details="${escapeHtml(d.desktop_id)}">详情</button>`+(registered?(a.online?`<button class="text-button" data-use="${a.id}" ${!connected?'disabled':''}>选择使用</button>`:`<button class="text-button" data-wake="${escapeHtml(d.desktop_id)}" ${!connected||submitting||busy||!m.cloud_configured||!selectable?'disabled':''}>${busy?'任务处理中':'唤醒并检查'}</button>`):'');
      return `<tr class="${checked?'selected-row':''}"><td class="table-checkbox"><input type="checkbox" aria-label="选择 ${escapeHtml(d.display_name)}" data-desktop="${escapeHtml(d.desktop_id)}" ${checked?'checked':''} ${submitting||!selectable||(!checked&&selected.size>=limit())?'disabled':''}></td><td><strong class="computer-name">${escapeHtml(d.display_name)}</strong><small>${escapeHtml(d.desktop_id)}</small></td><td><span class="cloud-label ${d.cloud_status==='Running'?'running':''}">${escapeHtml(VM.cloudNames[d.cloud_status]||'状态待同步')}</span></td><td>${pill(connected?status.text:'实时状态未确认',connected?status.tone:'neutral')}<small>${registered?'已接入':'待接入'}${registered?' · 心跳 '+VM.relative(a.last_seen,s.now):''}</small></td><td class="table-actions">${controls}</td></tr>`;
    }).join('');
    html(el('inventoryList'),visible.length?`<table><thead><tr><th scope="col" class="table-checkbox">选择</th><th scope="col">电脑名称</th><th scope="col">云电脑</th><th scope="col">连接与蓝牙状态</th><th scope="col">操作</th></tr></thead><tbody>${markup}</tbody></table>`:`<div class="empty-state"><strong>${records.length?'没有匹配的电脑':'还没有云电脑目录'}</strong>${records.length?'试试其他名称或筛选条件。':'保存阿里云连接后，点击刷新阿里云列表。'}</div>`);
    el('inventoryPageInfo').textContent=`第 ${inventoryPage} / ${pages} 页 · 共 ${filtered.length} 台 · 每页 ${pageSize} 台`;
    el('inventoryPrev').disabled=inventoryPage<=1;el('inventoryNext').disabled=inventoryPage>=pages;
    selectedLabel();
  }
  function renderJobs(s){
    const query=el('jobSearch').value.trim().toLocaleLowerCase(),filter=el('jobFilter').value,byId=new Map(rows().map(d=>[d.desktop_id,d]));
    const filtered=s.management.jobs.filter(j=>[byId.get(j.desktop_id)?.display_name,j.desktop_id,j.id].join(' ').toLocaleLowerCase().includes(query)&&(filter==='all'||filter==='active'&&activeStates.has(j.state)||filter==='attention'&&['failed','unknown'].includes(j.state)||filter==='done'&&['succeeded','skipped'].includes(j.state)));
    const pages=Math.max(1,Math.ceil(filtered.length/jobPageSize));jobPage=Math.min(pages,jobPage);
    const visible=filtered.slice((jobPage-1)*jobPageSize,jobPage*jobPageSize);
    html(el('jobList'),visible.length?`<table><thead><tr><th scope="col">电脑 / 时间</th><th scope="col">任务</th><th scope="col">进度</th><th scope="col">结果与处理</th></tr></thead><tbody>${visible.map(j=>{
      const attention=['failed','unknown'].includes(j.state),tone=attention?'bad':activeStates.has(j.state)?'warn':'good';
      let message=j.state==='succeeded'?(j.operation==='diagnose'?'诊断完成':j.operation==='repair'?'新版代理心跳已确认':'接入完成，已确认代理在线'):j.state==='skipped'?'保留现有身份，无需重复接入':helpers[j.error_code]||stages[j.stage]||'等待回读执行结果';
      return `<tr><td><strong class="computer-name">${escapeHtml(byId.get(j.desktop_id)?.display_name||j.desktop_id)}</strong><small>${escapeHtml(fmtTime(j.created_at))}</small></td><td>${escapeHtml(operations[j.operation]||'维护任务')}</td><td>${pill(states[j.state]||'状态待确认',tone)}<small>${escapeHtml(stages[j.stage]||'')}</small></td><td class="job-message ${attention?'job-error':''}">${escapeHtml(message)}<div class="table-actions">${j.state==='unknown'?`<button class="text-button" data-reconcile="${escapeHtml(j.id)}" ${!connected||submitting?'disabled':''}>核对原任务</button>`:j.state==='failed'?`<button class="text-button" data-retry="${escapeHtml(j.id)}" ${!connected||submitting?'disabled':''}>重试此台</button>`:''}</div><details class="job-technical"><summary>任务信息</summary><code>ID: ${escapeHtml(j.id)}${j.error_code?'<br>代码: '+escapeHtml(j.error_code):''}${j.invoke_id?'<br>执行 ID: '+escapeHtml(j.invoke_id):''}</code></details></td></tr>`;
    }).join('')}</tbody></table>`:'<div class="empty-state"><strong>暂无匹配任务</strong>任务提交后，可在这里查看执行结果和心跳确认。</div>');
    el('jobPageInfo').textContent=`第 ${jobPage} / ${pages} 页 · 共 ${filtered.length} 项`;
    el('jobPrev').disabled=jobPage<=1;el('jobNext').disabled=jobPage>=pages;
    const count=s.management.jobs.filter(j=>activeStates.has(j.state)).length;el('taskCount').hidden=!count;el('taskCount').textContent=count;
  }
  window.renderManagement=s=>{
    const m=s.management;if(!m)return;
    const monitor=m.watchdog,monitorText=monitor?.peer?(monitor.online?(monitor.startup_sent?'在线':'通知待确认'):'离线'):'未接入';
    el('integrationStatus').textContent=`阿里云 ${m.cloud_configured?'已连接':'待配置'}　·　Telegram ${m.telegram_configured?'已配置':'待配置'}　·　独立监测 ${connected?monitorText:'状态未确认'}`;
    el('watchdogSettingsStatus').textContent=monitor?.peer?`独立监测：${monitorText}。${monitor.last_seen?'最近心跳 '+fmtTime(monitor.last_seen)+'。':''}Mac 休眠或后台失联时由独立服务器提醒。`:'尚未配置独立监测；Mac 自身离线时需要独立服务器才能通知。';
    el('isolationStatus').textContent=!connected?'当前后台连接中断，任务操作暂不可用。':!m.source_isolation_configured?'首次接入前，请先完成源端配置。':!m.source_isolation_ready?'源端尚未就绪，请检查日常使用中的连接状态。':!m.bulk_ready?'首次接入暂未开放：请完成试点验证或最近故障后的复测。':`源端及试点验证通过，每批最多 ${limit()} 台，逐台执行。`;
    const issueCount=m.incidents.length+(m.failed_notifications?1:0);el('incidentPanel').hidden=!issueCount;el('incidentSummary').textContent=`有 ${issueCount} 项提醒需要查看`;
    html(el('incidentList'),m.incidents.map(i=>`<div class="incident"><div><strong>${escapeHtml(i.message)}</strong><p>${escapeHtml(i.code)} · ${escapeHtml(fmtTime(i.opened_at))}</p></div><button class="secondary small" data-ack="${escapeHtml(i.incident_key)}" ${!connected||i.acknowledged_at?'disabled':''}>${i.acknowledged_at?'已标记查看':'标记已查看'}</button></div>`).join('')+(m.failed_notifications?'<div class="incident"><strong>Telegram 有通知发送失败，请检查连接设置并发送测试通知。</strong></div>':'')||'<p class="hint">暂无未恢复的问题，当前运行提醒会显示在这里。</p>');
    renderInventory(s);renderJobs(s);
  };
  async function submit(ids,operation,batchId,originButton){
    if(submitting||!connected)return false;
    if(!ids.length){toast('请先勾选目标电脑');return false;}
    if(ids.length>limit()){toast(`每批最多 ${limit()} 台，请减少勾选数量`);return false;}
    submitting=true;redraw();if(originButton)originButton.disabled=true;
    try{await api('/api/jobs','POST',{desktop_ids:ids,operation,batch_id:batchId});submission=null;for(const id of ids)selected.delete(id);toast('任务已提交，后台会自动唤醒并执行');location.hash='tasks';setView('tasks');await refresh();return true;}catch(error){toast(error.message+'。若未取得回执，请先刷新任务列表核对。');return false;}finally{submitting=false;redraw();if(originButton?.isConnected)originButton.disabled=false;}
  }
  const wakeRequests=new Map();
  window.bridgeWake=async desktop=>{
    if(!state||!connected||submitting)return;
    const existing=state.management.jobs.find(j=>j.desktop_id===desktop&&(activeStates.has(j.state)||j.state==='unknown'));
    if(existing){location.hash='tasks';setView('tasks');toast('已有未完成任务，请先核对进度');return;}
    const prior=wakeRequests.get(desktop);
    if(prior&&state.management.jobs.some(j=>j.batch_id===prior&&!activeStates.has(j.state)&&j.state!=='unknown'))wakeRequests.delete(desktop);
    if(!wakeRequests.has(desktop))wakeRequests.set(desktop,crypto.randomUUID());
    if(await submit([desktop],'diagnose',wakeRequests.get(desktop)))wakeRequests.delete(desktop);
  };
  el('inventorySearch').oninput=()=>{inventoryPage=1;el('inventoryList').scrollTop=0;redraw();};el('inventoryFilter').onchange=()=>{inventoryPage=1;el('inventoryList').scrollTop=0;redraw();};
  el('inventoryPrev').onclick=()=>{inventoryPage--;el('inventoryList').scrollTop=0;redraw();};el('inventoryNext').onclick=()=>{inventoryPage++;el('inventoryList').scrollTop=0;redraw();};
  el('jobSearch').oninput=()=>{jobPage=1;el('jobList').scrollTop=0;redraw();};el('jobFilter').onchange=()=>{jobPage=1;el('jobList').scrollTop=0;redraw();};el('jobPrev').onclick=()=>{jobPage--;el('jobList').scrollTop=0;redraw();};el('jobNext').onclick=()=>{jobPage++;el('jobList').scrollTop=0;redraw();};
  el('inventoryList').addEventListener('change',event=>{const box=event.target.closest('[data-desktop]');if(!box)return;if(box.checked&&selected.size>=limit()){box.checked=false;toast(`每批最多 ${limit()} 台`);return;}box.checked?selected.add(box.dataset.desktop):selected.delete(box.dataset.desktop);submission=null;redraw();});
  el('inventoryList').addEventListener('click',event=>{const button=event.target.closest('button');if(!button||button.disabled)return;if(button.dataset.details)showDevice(button.dataset.details);else if(button.dataset.use)chooseTarget(button.dataset.use);else if(button.dataset.wake)window.bridgeWake(button.dataset.wake);});
  el('clearSelection').onclick=()=>{selected.clear();submission=null;redraw();};
  el('runBatch').onclick=()=>{if(!submission)submission={ids:[...selected].sort(),operation:el('batchOperation').value,batch_id:crypto.randomUUID()};return submit(submission.ids,submission.operation,submission.batch_id);};
  el('batchOperation').onchange=()=>{submission=null;selectedLabel();};
  el('jobList').addEventListener('click',async event=>{const button=event.target.closest('button');if(!button||button.disabled||!connected)return;if(button.dataset.reconcile){submitting=true;redraw();try{await api('/api/jobs/reconcile','POST',{job_id:button.dataset.reconcile});toast('已开始核对原执行任务');await refresh();}catch(error){toast(error.message);}finally{submitting=false;redraw();}}else if(button.dataset.retry){const job=state.management.jobs.find(j=>j.id===button.dataset.retry);if(job){if(!submission||submission.retry_id!==job.id)submission={ids:[job.desktop_id],operation:job.operation,batch_id:crypto.randomUUID(),retry_id:job.id};await submit(submission.ids,submission.operation,submission.batch_id);}}});
  el('incidentList').addEventListener('click',async event=>{const button=event.target.closest('[data-ack]');if(!button||button.disabled)return;button.disabled=true;try{await api('/api/incidents/ack','POST',{incident_key:button.dataset.ack});await refresh();}catch(error){toast(error.message);button.disabled=false;}});
  el('syncInventory').onclick=async()=>{const b=el('syncInventory');b.disabled=true;try{const result=await api('/api/inventory/sync','POST',{});toast(`已读取 ${result.count} 台云电脑`);await refresh();}catch(error){toast(error.message);}finally{b.disabled=false;}};
  function openSettings(){el('settingsDialog').showModal();}
  el('openSettings').onclick=openSettings;document.querySelectorAll('[data-open-settings]').forEach(b=>b.onclick=openSettings);
  el('closeSettings').onclick=()=>el('settingsDialog').close();
  el('settingsDialog').addEventListener('close',()=>{el('cloudSettings').reset();el('telegramSettings').reset();pairingCode='';el('telegramPairingCommand').value='';el('telegramPairingBox').hidden=true;el('discoverTelegramChat').hidden=true;});
  el('cloudSettings').onsubmit=async event=>{event.preventDefault();const b=event.submitter;b.disabled=true;try{await api('/api/settings/cloud','POST',{access_id:el('cloudAccessId').value,access_secret:el('cloudAccessSecret').value});event.target.reset();toast('阿里云连接已验证并保存到钥匙串');await refresh();}catch(error){toast(error.message);}finally{b.disabled=false;}};
  el('telegramSettings').onsubmit=async event=>{event.preventDefault();const b=event.submitter;b.disabled=true;try{await api('/api/settings/telegram','POST',{token:el('telegramToken').value,chat_id:el('telegramChatId').value});event.target.reset();toast('通知连接已验证并保存到钥匙串');await refresh();}catch(error){toast(error.message);}finally{b.disabled=false;}};
  el('testTelegram').onclick=async()=>{const b=el('testTelegram');b.disabled=true;try{await api('/api/settings/telegram/test','POST',{});toast('测试通知已发送，请查看手机');}catch(error){toast(error.message);}finally{b.disabled=false;}};
  el('createTelegramPairing').onclick=()=>{const bytes=crypto.getRandomValues(new Uint8Array(12));pairingCode='bridge-'+Array.from(bytes,x=>x.toString(16).padStart(2,'0')).join('');el('telegramPairingCommand').value='/start '+pairingCode;el('telegramPairingBox').hidden=false;el('discoverTelegramChat').hidden=false;el('telegramPairingCommand').select();toast('复制完整指令，用本人账号发给机器人');};
  el('discoverTelegramChat').onclick=async()=>{const b=el('discoverTelegramChat');b.disabled=true;try{const result=await api('/api/settings/telegram/discover','POST',{token:el('telegramToken').value,pairing_code:pairingCode});el('telegramChatId').value=result.chat_id;toast('已识别私聊，请验证并保存通知连接');}catch(error){toast(error.message);}finally{b.disabled=false;}};
  if(state)window.renderManagement(state);
})();
