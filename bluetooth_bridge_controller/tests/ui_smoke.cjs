/* Isolated browser regression tests. Every request is fulfilled locally; no live API calls. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const staticRoot=path.resolve(__dirname,'../static');
const output=process.env.UI_OUTPUT||'/private/tmp/bluetooth-bridge-ui';
fs.mkdirSync(output,{recursive:true});
const now=Math.floor(Date.now()/1000);
function fixture(){
 const inventory=Array.from({length:101},(_,i)=>({desktop_id:`ecd-${String(i+1).padStart(3,'0')}`,display_name:i===0?'Yorke':i===1?'张26':i===2?'<img src=x onerror=alert(1)>':`电脑${String(i+1).padStart(3,'0')}`,cloud_status:i===1?'Stopped':'Running',updated_at:now}));
 const agents=[1,2].map(id=>({id,desktop_id:inventory[id-1].desktop_id,display_name:inventory[id-1].display_name,machine_id:'registered',hostname:'PC-'+id,tailscale_ip:'100.64.0.'+id,last_seen:now,online:id===1,status:'released',last_report:{vh_status:'released',bluetooth_status:'absent',error:''}}));
 return {ok:true,now,agents,events:[{created_at:now,event:'phone_passkey_verified',agent_id:2,level:'info'}],lease:{state:'idle',target_id:null,pending_target_id:null,expires_at:null,reset_error:'',generation:9},config:{usb:'10d7:b012',lease_seconds:900},management:{inventory,jobs:[{id:'job-complete',desktop_id:'ecd-001',operation:'diagnose',state:'succeeded',stage:'diagnostic_complete',error_code:'Configured',created_at:now}],incidents:[],failed_notifications:0,source_hardware:{count:1,checked_at:now},source_isolation_ready:true,source_isolation_configured:true,source_reset_ready:true,source_reset_configured:true,bulk_ready:true,max_batch_size:3,cloud_configured:true,telegram_configured:true,watchdog:{peer:'100.64.0.9',online:true,startup_sent:true,last_seen:now},usb_active_tunnels:0}};
}
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 const passed=[],errors=[];
 async function session(data=fixture(),viewport={width:1440,height:1000}){
  const context=await browser.newContext({viewport,locale:'zh-CN'}),page=await context.newPage(),posts=[];
  const mode={statusFailure:false,jobAbortOnce:false};
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',async route=>{
   const request=route.request(),url=new URL(request.url());
   if(url.pathname.startsWith('/api/')){
    if(request.method()==='POST'){
     posts.push({path:url.pathname,body:request.postDataJSON()});
     if(url.pathname==='/api/jobs'&&mode.jobAbortOnce){mode.jobAbortOnce=false;return route.abort('failed');}
     return route.fulfill({json:{ok:true,job_ids:['test-job'],count:101}});
    }
    if(mode.statusFailure)return route.fulfill({status:503,json:{ok:false,error:'测试后台暂不可达'}});
    return route.fulfill({json:data});
   }
   const name=url.pathname==='/'?'index.html':url.pathname.slice(1);
   if(!['index.html','styles.css','management.css','view-model.js','app.js','management.js'].includes(name))return route.fulfill({status:404,body:''});
   return route.fulfill({body:fs.readFileSync(path.join(staticRoot,name)),contentType:name.endsWith('.css')?'text/css':name.endsWith('.js')?'text/javascript':'text/html'});
  });
  await page.goto('http://127.0.0.1:18766/');
  await page.waitForFunction(count=>document.querySelector('#registeredDevices').textContent===String(count),data.agents.filter(a=>a.machine_id).length);
  return {page,context,posts,data,mode};
 }
 let s=await session();
 await s.page.selectOption('#targetSelect','1');
 assert.equal(await s.page.locator('#startButton').isEnabled(),true);
 assert.match(await s.page.locator('#adapterStatus').innerText(),/已插入/);
 assert.match(await s.page.locator('#connectionPath').innerText(),/由手机与网站确认/);
 await s.page.screenshot({path:path.join(output,'daily-desktop.png'),fullPage:true});
 passed.push('Idle view shows source and driver layers without claiming phone connection');
 await s.page.selectOption('#targetSelect','2');await s.page.locator('#startButton').click();
 await s.page.waitForFunction(()=>location.hash==='#tasks');
 assert.equal(s.posts.filter(p=>p.path==='/api/lease/acquire').length,0);
 assert.deepEqual(s.posts.find(p=>p.path==='/api/jobs').body.desktop_ids,['ecd-002']);
 assert.equal(s.posts.find(p=>p.path==='/api/jobs').body.operation,'diagnose');
 passed.push('Offline start requests a wake/diagnostic, never prematurely acquires USB');
 await s.context.close();
 s=await session();await s.page.locator('nav [data-view=devices]').click();
 const boxes=s.page.locator('#inventoryList [data-desktop]');assert.equal(await boxes.count(),20);
 assert.equal(await s.page.locator('#inventoryList img').count(),0);
 const first=await boxes.first().getAttribute('data-desktop');await boxes.first().check();
 await s.page.locator('#inventoryNext').click();const other=s.page.locator('#inventoryList [data-desktop]');const second=await other.nth(0).getAttribute('data-desktop');const third=await other.nth(1).getAttribute('data-desktop');await other.nth(0).check();await other.nth(1).check();
 assert.equal(await other.nth(2).isDisabled(),true);
 await s.page.locator('#inventorySearch').fill('不存在的设备');assert.match(await s.page.locator('#inventoryList').innerText(),/没有匹配/);assert.match(await s.page.locator('#selectionCount').innerText(),/已选 3 台/);
 await s.page.locator('#inventorySearch').fill('');await s.page.evaluate(()=>refresh());
 assert.equal(await s.page.locator(`#inventoryList [data-desktop="${first}"]`).isChecked(),true);
 assert.equal(await s.page.locator('#selectedNames .selection-chip').count(),3);
 await s.page.locator(`#inventoryList [data-desktop="${first}"]`).focus();s.data.now+=3;await s.page.evaluate(()=>refresh());
 assert.equal(await s.page.evaluate(()=>document.activeElement.dataset.desktop),first);
 await s.page.screenshot({path:path.join(output,'devices-desktop.png'),fullPage:true});
 s.mode.jobAbortOnce=true;await s.page.locator('#runBatch').click();
 await s.page.waitForFunction(()=>document.querySelector('#runBatch').textContent==='执行已选任务');assert.equal(s.posts.length,1);
 await s.page.locator('#runBatch').click();await s.page.waitForFunction(()=>location.hash==='#tasks');
 assert.equal(s.posts.length,2);assert.equal(s.posts[0].body.batch_id,s.posts[1].body.batch_id);assert.deepEqual(s.posts[1].body.desktop_ids,[first,second,third].sort());
 passed.push('101 devices paginate; selections survive search, page changes and polling; batch cap enforced');
 passed.push('Lost batch response retries reuse the same idempotency key and target snapshot');
 await s.context.close();
 const active=fixture();active.lease={state:'active',target_id:1,pending_target_id:null,session_id:'test-session',generation:10,expires_at:now+900,reset_error:''};active.agents[0].last_report={vh_status:'attached',bluetooth_status:'ready',error:''};active.management.usb_active_tunnels=1;
 s=await session(active);await s.page.clock.install();
 assert.match(await s.page.locator('#currentTarget').innerText(),/正在使用/);assert.equal(await s.page.locator('#releaseButton').isEnabled(),true);
 const renewed=s.page.waitForRequest(r=>r.url().endsWith('/api/lease/renew'));await s.page.clock.fastForward(11000);await renewed;
 await s.page.screenshot({path:path.join(output,'active-desktop.png'),fullPage:true});
 s.mode.statusFailure=true;await s.page.evaluate(()=>refresh());
 assert.match(await s.page.locator('#systemPill').innerText(),/连接中断/);assert.match(await s.page.locator('#adapterStatus').innerText(),/未确认/);assert.equal(await s.page.locator('#startButton').isDisabled(),true);assert.equal(await s.page.locator('#releaseButton').isDisabled(),true);
 const renewBefore=s.posts.filter(p=>p.path==='/api/lease/renew').length;await s.page.clock.fastForward(11000);assert.equal(s.posts.filter(p=>p.path==='/api/lease/renew').length,renewBefore);
 passed.push('Active sessions renew with their generation; backend failure clears live readiness and stops renewal');
 s.mode.statusFailure=false;active.agents[0].last_report.error='USB test fault';await s.page.evaluate(()=>refresh());assert.match(await s.page.locator('#remoteBluetooth').innerText(),/异常/);assert.doesNotMatch(await s.page.locator('#currentTarget').innerText(),/正在使用/);
 active.agents[0].last_report.error='';active.management.source_isolation_ready=false;await s.page.evaluate(()=>refresh());assert.match(await s.page.locator('#currentTarget').innerText(),/共享通道需要检查/);
 active.management.source_isolation_ready=true;active.agents[0].online=false;await s.page.evaluate(()=>refresh());assert.match(await s.page.locator('#remoteBluetooth').innerText(),/状态待确认/);assert.doesNotMatch(await s.page.locator('#currentTarget').innerText(),/正在使用/);
 passed.push('Old ready reports from an offline agent cannot display a ready shared connection');
 await s.context.close();
 const missing=fixture();missing.management.source_hardware.count=0;missing.management.source_isolation_ready=false;
 s=await session(missing);assert.equal(await s.page.locator('#startButton').isDisabled(),true);assert.match(await s.page.locator('#currentTarget').innerText(),/USB 蓝牙需要检查/);passed.push('Missing adapter blocks use and displays a source fault');await s.context.close();
 s=await session(fixture(),{width:390,height:844});
 assert.equal(await s.page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);await s.page.screenshot({path:path.join(output,'daily-mobile.png'),fullPage:true});
 await s.page.locator('nav [data-view=devices]').click();assert.equal(await s.page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 await s.page.locator('#inventorySearch').fill('张26');assert.equal(await s.page.locator('#inventoryList tbody tr').count(),1);
 await s.page.locator('[data-details="ecd-002"]').click();assert.match(await s.page.locator('#deviceDetails').innerText(),/已关机/);await s.page.locator('#closeDeviceDialog').click();
 await s.page.locator('#openSettings').click();await s.page.locator('#telegramToken').fill('dummy-test-value');await s.page.keyboard.press('Escape');await s.page.locator('#openSettings').click();assert.equal(await s.page.locator('#telegramToken').inputValue(),'');
 passed.push('Mobile layout has no page overflow; search/details work; closing settings clears secret fields');
 await s.context.close();
 assert.deepEqual(errors,[]);
 if(process.env.UI_SNAPSHOT){
  const data=JSON.parse(fs.readFileSync(process.env.UI_SNAPSHOT,'utf8'));s=await session(data);await s.page.waitForTimeout(100);await s.page.screenshot({path:path.join(output,'live-snapshot-desktop.png'),fullPage:true});await s.context.close();
 }
 await browser.close();fs.writeFileSync(path.join(output,'results.json'),JSON.stringify({passed,screenshots:output},null,2));console.log(JSON.stringify({passed,screenshots:output},null,2));
})().catch(error=>{console.error(error);process.exit(1);});
