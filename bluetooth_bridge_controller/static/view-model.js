/* Display-only status interpretation. USB authority stays with the controller. */
(() => {
  const cloudNames={Running:'运行中',Stopped:'已关机',Hibernated:'已休眠',Starting:'启动中',Stopping:'关机中',Pending:'创建中',Rebooting:'重启中',Unknown:'待同步'};
  const sleeping=cloud=>['Stopped','Hibernated'].includes(cloud);
  const age=(timestamp,current)=>timestamp?Math.max(0,current-timestamp):Infinity;
  function relative(timestamp,current){const seconds=age(timestamp,current);return !Number.isFinite(seconds)?'尚无回报':seconds<60?`${Math.floor(seconds)} 秒前`:seconds<3600?`${Math.floor(seconds/60)} 分钟前`:`${Math.floor(seconds/3600)} 小时前`;}
  function records(s){
    const rows=new Map((s.management?.inventory||[]).map(d=>[d.desktop_id,{...d}]));
    for(const agent of s.agents){
      const row=rows.get(agent.desktop_id)||{desktop_id:agent.desktop_id,display_name:agent.display_name,cloud_status:'Unknown'};
      row.agent=agent;rows.set(agent.desktop_id,row);
    }
    return [...rows.values()].sort((a,b)=>(a.display_name||'').localeCompare(b.display_name||'','zh-CN',{numeric:true}));
  }
  function device(agent,cloud,lease){
    if(!agent?.machine_id)return {tone:'neutral',text:'待接入',description:'接入后可读取蓝牙状态并分配使用。'};
    if(!agent.online)return sleeping(cloud)?{tone:'neutral',text:cloudNames[cloud],description:'已接入，使用前唤醒电脑，无需重复登记。'}:{tone:'warn',text:'代理离线',description:'尚未收到新心跳，请检查桌面和网络连接。'};
    if(agent.last_report?.error)return {tone:'bad',text:'连接异常',description:'代理回报错误，可在电脑详情中查看并读取诊断。'};
    if(lease.state==='resetting'&&lease.pending_target_id===agent.id)return {tone:'warn',text:'正在准备蓝牙',description:'准备完成后会自动连接，请稍候。'};
    if(lease.target_id===agent.id&&lease.state==='releasing')return {tone:'warn',text:'正在释放',description:'正在等待原电脑确认 USB 释放及蓝牙消失。'};
    const report=agent.last_report||{};
    if(lease.target_id===agent.id&&lease.state==='active'&&report.vh_status==='attached'&&report.bluetooth_status==='ready')return {tone:'good',text:'蓝牙就绪 · 共享中',description:'云电脑已识别适配器，可以发起手机通行密钥验证。'};
    if(report.vh_status==='attached')return {tone:'warn',text:'连接待确认',description:'USB 已连接，尚未满足全部就绪条件。'};
    if(report.vh_status==='preparing'||(lease.target_id===agent.id&&lease.state==='authorizing'))return {tone:'warn',text:'正在连接蓝牙',description:'等待 Windows 识别适配器，请保持桌面连接。'};
    if(report.vh_status==='released'&&report.bluetooth_status==='absent')return {tone:'good',text:'在线待用',description:'代理在线，蓝牙已释放，选择后可以开始使用。'};
    return {tone:'warn',text:'等待状态确认',description:'代理在线，等待 USB 和蓝牙状态回报。'};
  }
  function source(s,fresh){
    if(!fresh)return {adapter:{tone:'neutral',text:'状态未确认'},bridge:{tone:'neutral',text:'状态未确认'}};
    const m=s.management||{}, hardware=m.source_hardware||{};
    const recent=age(hardware.checked_at,s.now)<=75;
    const adapter=!recent?{tone:'warn',text:'等待重新检查'}:hardware.count===1?{tone:'good',text:'已插入 · 1 只适配器'}:hardware.count===0?{tone:'bad',text:'未检测到适配器'}:{tone:'bad',text:'检测到多只同型适配器'};
    const bridge=s.lease.state==='resetting'?{tone:'warn',text:'正在准备通道'}:m.source_isolation_ready?{tone:'good',text:'通道已就绪'}:{tone:'bad',text:'通道尚未就绪'};
    return {adapter,bridge};
  }
  globalThis.BridgeViewModel={cloudNames,sleeping,relative,records,device,source};
})();
