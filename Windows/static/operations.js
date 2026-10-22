"use strict";
window.Operations=(()=>{
  let latest=null, activityBefore=0, confirmHandler=null;
  const time=(stamp)=>stamp?new Date(stamp*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'}):'—';
  const duration=(seconds)=>seconds>=3600?`${Math.floor(seconds/3600)} 小时 ${Math.floor(seconds%3600/60)} 分钟`:`${Math.max(0,Math.floor(seconds/60))} 分钟`;
  const node=(tag,text,cls)=>{const el=document.createElement(tag);el.textContent=text;if(cls)el.className=cls;return el;};
  function confirm(title,text,handler){
    confirmHandler=handler;$('operation-confirm-title').textContent=title;$('operation-confirm-text').textContent=text;$('operation-confirm').showModal();$('operation-cancel').focus();
  }
  $('operation-cancel').onclick=()=>$('operation-confirm').close();
  $('operation-accept').onclick=action(async()=>{const fn=confirmHandler;$('operation-confirm').close();confirmHandler=null;if(fn)await fn();});
  $('operation-confirm').addEventListener('close',()=>confirmHandler=null);
  function update(data){
    latest=data;const op=data.operations;if(!op)return;
    $('console-date').textContent=new Date().toLocaleDateString('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'long',day:'numeric',weekday:'long'});
    $('run-label').textContent=data.offline?'离线验证环境':data.config.paused?'机器人已暂停':op.ready?'接入检查通过 · 等待消息':'尚未就绪';
    $('run-indicator').classList.toggle('paused',!op.ready);
    $('readiness-detail').textContent=data.config.paused?'停止新的聊天响应与主动检查，管理员私聊仍可管理。':op.ready?'仅参与已授权会话；是否回复由模型决定。':op.checks.filter(c=>!c.ok).map(c=>c.title).join('、')+'尚未完成。';
    if(op.blockers?.length&&!data.offline)$('readiness-detail').textContent=op.blockers.join('；')+'。';
    if(op.model.retry_at>Date.now()/1000)$('readiness-detail').textContent='模型连接保护中，'+time(op.model.retry_at)+' 后允许重试。';
    $('setup-progress').textContent=op.checks.filter(c=>c.ok).length+' / '+op.checks.length;
    const checks=$('readiness-checks');checks.replaceChildren();op.checks.forEach((check,index)=>{
      const row=node('button','',`readiness-item ${check.ok?'complete':''}`);row.type='button';row.append(node('span',check.ok?'✓':String(index+1),'check-number'));
      const text=node('div','');text.append(node('strong',check.title),node('small',check.ok?'已完成':check.hint));row.append(text,node('span',check.ok?'已完成':'去设置 ↗','check-action'));row.onclick=()=>page(check.page);checks.append(row);
    });
    const metrics=op.metrics;
    $('health-success').textContent=metrics.total?Math.round(metrics.success/metrics.total*100)+'%':'暂无请求';
    $('health-latency').textContent=metrics.total?metrics.latency.toFixed(1)+' 秒':'—';
    $('health-queue').textContent=op.workers+' / '+op.queue;
    $('health-uptime').textContent=duration(op.uptime);
    $('runtime-state').textContent=data.offline?'离线验证环境，不操作真实服务':data.bridge.online?'QQ 在线 · '+data.bridge.self_id:data.bridge.connected?'消息通道已连，正在确认账号状态':op.runtime_installed?'独立环境目录已存在 · QQ 未连接':'尚未安装 QQ 接入环境';
    $('shutdown-all').disabled=data.offline||shutdownInProgress;
    const busy=op.job?.status==='running';document.querySelectorAll('[data-runtime]').forEach(b=>b.disabled=busy||data.offline);
    $('runtime-job').hidden=!op.job;
    if(op.job){const names={install:'安装 / 检查',start:'启动接入',stop:'停止接入'},statuses={running:'正在执行',succeeded:'已完成',failed:'未完成，可检查后重试',interrupted:'已中断，可重新执行'};
      $('runtime-job-label').textContent=names[op.job.command]+' · '+statuses[op.job.status];$('runtime-job-time').textContent=time(op.job.started);$('runtime-job-log').textContent=op.job.lines.join('\n')||'任务已提交，等待环境响应…';
      const lines=op.job.lines||[];const phase=[...lines].reverse().find(line=>line.startsWith('阶段：'));
      const networkFailure=lines.some(line=>/failed to resolve reference|failed to do request|TLS handshake|unexpected EOF|: EOF/.test(line));
      $('runtime-job-summary').textContent=busy?(phase||'正在准备独立环境。请等待 Docker 容器与接入组件检查完成。'):op.job.status==='succeeded'?(op.job.command==='stop'?'QQ 环境已停止。管理界面仍可使用。':data.bridge.online?'QQ 接入准备完成，账号已在线。':'环境准备完成。下一步点击「扫码登录 QQ」，用机器人账号在手机上确认。'):networkFailure?'接入组件下载失败：连接镜像仓库中断。本次 Docker 接入检查没有完成；可重试安装 / 检查。':'本次操作未完成。请展开技术日志查看原因，再重试。';
      $('runtime-login').hidden=busy||op.job.status!=='succeeded'||op.job.command==='stop'||data.bridge.online;
      $('runtime-retry').hidden=busy||!['failed','interrupted'].includes(op.job.status);
}
    $('storage-state').textContent=(op.integrity?'数据库检查通过':'数据库检查未通过')+' · 数据库及日志 '+((op.storage?.total||0)/1024**2).toFixed(1)+' MB · 磁盘可用 '+(op.free_bytes/1024**3).toFixed(1)+' GB'+(op.warnings?.length?' · '+op.warnings.join('；'):'');
    $('model-queue-state').textContent='正在请求 '+(op.model.requests||0)+' · 排队 '+(op.model.waiting||0)+'；聊天优先，后台整理最多占一个请求名额。';
    const taskNames={bridge:'QQ 连接',scheduler:'聊天调度',maintenance:'数据维护',memory:'记忆整理'};
    $('background-state').textContent=Object.entries(op.tasks||{}).map(([name,t])=>(taskNames[name]||name)+'：'+({running:'运行中',retrying:'故障后重试',stopped:'已停止',starting:'启动中'}[t.state]||t.state)+(t.restarts?'（重试计数 '+t.restarts+'）':'')).join('；')||(data.offline?'离线测试不启动后台任务':'正在启动后台任务');
    $('safe-error-traces').textContent=(op.errors||[]).map(e=>time(e.time)+' · '+e.kind+' · '+e.id+'\n'+e.trace).join('\n\n')||'暂无异常位置记录。';
    $('model-state').textContent=op.model.retry_at>Date.now()/1000?'暂时保护至 '+time(op.model.retry_at):op.model.last_error|| (op.model.last_success?'最近成功：'+time(op.model.last_success):'尚无连接成功记录；请手动测试');
    const list=$('delivery-list');list.replaceChildren();if(!op.deliveries.length)list.append(node('p','还没有发送记录。接入并授权会话后，每次发送会在这里留下回执。','empty'));
    op.deliveries.forEach(item=>{const row=node('article','', 'record-row');const info=node('div','');info.append(node('strong',data.config.conversations[item.chat]?.name||item.chat),node('small',time(item.time)+' · '+({reply:'聊天回复',proactive:'主动聊天',admin:'管理员反馈'}[item.kind]||item.kind)));row.append(info,node('span',{pending:'等待回执',confirmed:'接口已确认',unknown:'结果不确定'}[item.status]||item.status,'pill '+(item.status==='confirmed'?'online':'attention')));list.append(row);});
    const backups=$('backup-list');backups.replaceChildren();if(!op.backups.length)backups.append(node('p','还没有备份。创建第一份经过校验的本地快照。','empty'));
    op.backups.forEach(item=>{const row=node('article','','record-row');const info=node('div','');info.append(node('strong',time(item.time)),node('small',({manual:'手动备份',daily:'每日自动备份',daily_startup:'启动时自动备份',before_restore:'恢复前保护备份'}[item.reason]||item.reason)+' · '+(item.bytes/1024**2).toFixed(2)+' MB'),node('small',item.id));const button=node('button','恢复这份备份','secondary');button.onclick=()=>confirm('恢复本地备份？','将恢复 '+time(item.time)+' 的角色、会话设置和记忆。需要先暂停机器人并等待正在处理的请求结束。当前账号和密钥保留，恢复后仍保持暂停。',async()=>{await api('backups/restore',{id:item.id,revision:state.config.revision});await refresh();toast('备份已恢复，机器人保持暂停，请核对配置后恢复运行');});row.append(info,button);backups.append(row);});
  }
  document.querySelectorAll('[data-runtime]').forEach(button=>button.onclick=action(async()=>{
    const command=button.dataset.runtime;const execute=async()=>{await api('runtime',{command});toast('环境任务已提交，可在此查看进度');await refresh(false);};
    if(command==='stop')confirm('停止 QQ 接入？','会中断 QQ 连接与正在等待回执的发送。管理界面继续运行，需要时可以重新启动。',execute);
    else if(command==='start'&&latest?.bridge.connected)confirm('重新启动 QQ 接入？','配置未变化时会复用当前接入；配置变化时会更新容器，QQ 登录数据会保留。',execute);
    else await execute();
  }));
  $('shutdown-all').onclick=()=>confirm('关闭机器人？','将停止 QQ 环境和管理服务，当前未保存的修改不会保存。关闭后不再回复或主动发消息；下次双击「打开机器人.command」即可进入。',async()=>{
    shutdownInProgress=true;$('shutdown-all').disabled=true;
    try{
      const job=await api('runtime',{command:'stop'});
      toast('正在关闭 QQ 环境，请等待完成…');
      while(true){
        await new Promise(resolve=>setTimeout(resolve,1000));
        const data=await api('status?light=1');update(data);
        const current=data.operations.job;
        if(current?.id!==job.id)throw new Error('环境任务已变化，请检查服务状态后重试关闭。');
        if(current.status==='running')continue;
        if(current.status!=='succeeded')throw new Error('QQ 环境未确认停止，管理界面保持运行。请在「运行与数据」查看错误后重试。');
        break;
      }
      await api('shutdown',{});
      $('shell').hidden=true;$('login').hidden=true;
      const panel=node('main','','card');panel.append(node('h1','机器人已关闭'),node('p','QQ 环境已停止，管理服务正在退出。稍后可删除整个文件夹；下次使用请双击「打开机器人.command」。'));
      document.body.append(panel);state=null;
    }catch(error){shutdownInProgress=false;$('shutdown-all').disabled=false;throw error;}
  });
  $('runtime-login').onclick=()=>window.QQLogin.open();
  $('runtime-retry').onclick=action(async()=>{await api('runtime',{command:'install'});await refresh(false);});
  $('create-backup').onclick=action(async()=>{await api('backups',{});await refresh(false);toast('本地备份完成，已通过完整性校验');});
  function show(tab){document.querySelectorAll('.ops-panel').forEach(el=>el.hidden=el.id!=='ops-'+tab);document.querySelectorAll('[data-ops-tab]').forEach(b=>{const active=b.dataset.opsTab===tab;b.setAttribute('aria-selected',String(active));b.tabIndex=active?0:-1;});if(tab==='activity')loadActivity().catch(e=>toast(e.message,true));}
  const tabButtons=[...document.querySelectorAll('[data-ops-tab]')];
  tabButtons.forEach((button,index)=>{button.onclick=()=>show(button.dataset.opsTab);button.id='ops-tab-'+button.dataset.opsTab;button.setAttribute('aria-controls','ops-'+button.dataset.opsTab);button.tabIndex=index===0?0:-1;const panel=$('ops-'+button.dataset.opsTab);panel.setAttribute('role','tabpanel');panel.setAttribute('aria-labelledby',button.id);
    button.onkeydown=event=>{let next=index;if(event.key==='ArrowRight')next=(index+1)%tabButtons.length;else if(event.key==='ArrowLeft')next=(index+tabButtons.length-1)%tabButtons.length;else if(event.key==='Home')next=0;else if(event.key==='End')next=tabButtons.length-1;else return;event.preventDefault();show(tabButtons[next].dataset.opsTab);tabButtons[next].focus();};});
  async function loadActivity(more=false){const result=await api('activity?kind='+encodeURIComponent($('activity-filter').value)+'&before='+(more?activityBefore:0));if(!more)$('activity-list').replaceChildren();activityBefore=result.before;
    if(!more&&!result.items.length)$('activity-list').append(node('p','暂无符合条件的运行记录。','empty'));
    result.items.forEach(item=>{const row=node('div','','audit-row');row.append(node('time',time(item.time)),node('span',auditLabels[item.kind]||item.kind),node('span',item.detail));$('activity-list').append(row);});$('activity-more').hidden=!result.more;}
  $('activity-refresh').onclick=action(()=>loadActivity());$('activity-filter').onchange=action(()=>loadActivity());$('activity-more').onclick=action(()=>loadActivity(true));
  return {update,confirm,show};
})();
