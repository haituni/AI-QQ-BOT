'use strict';
window.LongMemory=(()=>{
  let snapshot=null,chat='',busy=false;
  const el=(tag,text,cls)=>{const n=document.createElement(tag);n.textContent=text;if(cls)n.className=cls;return n;};
  const date=t=>t?new Date(t*1000).toLocaleString('zh-CN'):'—';
  function sync(data){
    const select=$('long-memory-chat'),previous=select.value;
    const options=Object.entries(data.config.conversations).map(([key,c])=>[key,c.name||key]);
    if(JSON.stringify(options)!==select.dataset.options){select.replaceChildren(new Option('选择一个会话',''));options.forEach(([key,name])=>select.add(new Option(name,key)));select.value=previous;select.dataset.options=JSON.stringify(options);}
    $('long-memory-consolidate').disabled=data.offline||busy||!select.value;
  }
  async function load(){
    chat=$('long-memory-chat').value;
    const data=await api('long-memory?chat='+encodeURIComponent(chat)+'&query='+encodeURIComponent($('long-memory-query').value));
    const form=$('memory-settings-form');
    if(form.dataset.dirty!=='true')snapshot=data.settings;
    $('long-memory-reset').disabled=!chat;
    if(form.dataset.dirty!=='true')Object.entries(snapshot).forEach(([key,value])=>{const input=form.elements[key];if(input){if(input.type==='checkbox')input.checked=value;else input.value=value;}});
    $('long-memory-state').textContent=chat?`QQ ${data.owner||'尚无发言'} · ${data.automatic?'自动整理已开启':'自动整理已暂停'} · ${data.active?'后台整理中 · ':''}已归档 ${data.archive_count.toLocaleString()} 条用户原话 · 待整理 ${data.pending_users} · 本会话上次整理 ${date(data.cursor.last_time)}${data.cursor.error?' · 上次整理失败：'+data.cursor.error:''}`:'先选择会话，查看当前用户。相同 QQ 跨会话共享记忆与磨合；不同 QQ 分开，角色原始人设保持稳定。';
    $('long-memory-consolidate').disabled=!chat||busy||state?.offline;
    const affect=data.emotion||{state:{plan:{}},global_enabled:false,subject:''},e=affect.state,p=e.plan,box=$('emotion-state');box.replaceChildren();
    $('emotion-enabled').checked=affect.global_enabled;
    $('emotion-enabled').disabled=!chat||!data.emotion;$('emotion-reset').disabled=!chat||!affect.subject;
    if(affect.subject){
      box.append(el('p',`QQ ${affect.subject} · ${affect.global_enabled?e.labels.join('、'):'情绪系统已关闭'} · 更新 ${date(e.updated)}`));
      const names={pleasure:'愉快',energy:'活力',tension:'紧张',warmth:'亲近',trust:'信任',attachment:'依恋',security:'关系安全感',flutter:'心动值',mood:'持续心境'};
      Object.entries(names).forEach(([key,name])=>{const row=el('div','','memory-note'),meter=document.createElement('meter');const value=e[key]??0;meter.min=['tension','flutter'].includes(key)?0:-1;meter.max=key==='flutter'?100:1;meter.value=value;meter.setAttribute('aria-label',name);row.append(el('span',`${name} ${key==='flutter'?value.toFixed(1)+' / 100':value.toFixed(2)} `),meter);box.append(row);});
      e.events.slice(-3).reverse().forEach(v=>box.append(el('p',`${date(v.time)} · ${v.reason}${v.event?' · 在意：'+v.event:''}`)));
      if(p.time)box.append(el('p',`最近计划：${p.delays.length?'气泡等待 '+p.delays.join(' / ')+' 秒':'本轮保持安静'}；${p.next_minutes} 分钟后检查主动联系。${p.reason||''}`));
      box.append(el('p',`预计发送：${date(affect.reply_at)} · 下次主动检查：${state?.config.conversations[chat]?.proactive?date(affect.next_at):'未开启主动聊天'}`,'muted'));
    }else box.append(el('p',data.emotion?'选择有真实发言者的会话后查看。尚无个人情绪时按角色的平静状态开始。':'情绪模块已准备好，等待管理服务重启后生效。','muted'));
    const list=$('long-memory-items');list.replaceChildren();
    if(!data.notes.length)list.append(el('p','还没有压缩记忆。原文仍可检索；达到批次阈值或聊天空闲后，会自动在后台提取，无需手动操作。','muted'));
    data.notes.forEach(n=>{const row=el('article','','memory-note');row.append(el('strong',n.topic),el('span',{active:'使用中',superseded:'历史版本',forgotten:'已停用'}[n.status]||n.status,'pill'),el('p',n.body),el('small',`QQ ${n.subject} · ${date(n.created)} · 来源编号 ${n.sources.join('、')}`));const quote=el('details','');quote.append(el('summary','查看原话证据'));n.quotes.forEach(q=>quote.append(el('blockquote',q)));row.append(quote);if(n.status==='active'){const button=el('button','停用此条检索','text-button');button.onclick=()=>window.Operations.confirm('停用这条记忆？','压缩记忆和关联原文将不再被长期检索选中；近期聊天、原始归档及备份仍保留。清除全部内容请使用会话的清除记忆操作。',async()=>{await api('long-memory',{action:'forget',chat:n.chat,id:n.id});await load();});row.append(button);}list.append(row);});
    const retrieval=$('long-memory-retrieval');retrieval.replaceChildren();
    data.retrieval.items.forEach(item=>{const row=el('article','','memory-note');row.append(el('strong',({fact:'事实',preference:'偏好',episode:'事件'}[item.type]||item.type)+' · QQ '+item.subject),el('p',item.text));retrieval.append(row);});
    if(!data.retrieval.items.length)retrieval.append(el('p','没有命中。这里采用本地关键词与中文词片检索，换一种问法可能得到不同结果。','muted'));
    const adaptation=$('long-memory-adaptation');adaptation.replaceChildren();
    data.retrieval.adaptation.forEach(item=>adaptation.append(el('p',item.text)));
    if(!data.retrieval.adaptation.length)adaptation.append(el('p','尚未形成稳定磨合倾向。不会因为一两次反馈立刻改变。','muted'));
    adaptation.append(el('small',`已记录 ${data.observations.length} 条近期观察。表达倾向至少4条证据、跨3天且跨度≥7天；每7天最多变化0.1，总偏移≤0.3。兴趣需要跨3天、跨度≥7天。同一 QQ 的群聊和私聊共用缓慢磨合；群里不带出私聊兴趣。`));
    data.observations.slice(0,8).forEach(o=>adaptation.append(el('p',`${o.day} · ${o.dimension}：${o.value} · 证据 ${o.source}`,'muted')));
  }
  $('emotion-refresh').onclick=action(load);
  $('emotion-enabled').onchange=action(async()=>{await api('long-memory',{action:'emotion-enabled',chat:$('long-memory-chat').value,enabled:$('emotion-enabled').checked});await load();});
  $('emotion-reset').onclick=()=>window.Operations.confirm('重置这个人的情绪？','清除此角色对此 QQ 的跨会话情绪和时间计划，保留聊天、事实记忆及长期磨合；待发回复会取消。',async()=>{await api('long-memory',{action:'emotion-reset',chat:$('long-memory-chat').value});await load();});
  $('memory-settings-form').oninput=()=>{$('memory-settings-form').dataset.dirty='true';};
  $('memory-settings-form').onsubmit=action(async()=>{const form=$('memory-settings-form'),patch={};Object.keys(snapshot||{}).forEach(key=>{const input=form.elements[key];if(input)patch[key]=input.type==='checkbox'?input.checked:Number(input.value);});await api('long-memory',{action:'settings',patch,previous:snapshot});form.dataset.dirty='false';await load();toast('长期记忆设置已保存');});
  $('memory-reload').onclick=()=>window.Operations.confirm('重新载入记忆策略？','未保存的策略修改将被放弃。',async()=>{$('memory-settings-form').dataset.dirty='false';await load();});
  $('long-memory-chat').onchange=action(load);$('long-memory-search').onclick=action(load);
  $('long-memory-consolidate').onclick=()=>window.Operations.confirm('整理一批记忆？','会调用一次 DeepSeek，从尚未整理的用户原话中提取记忆，计入模型次数和 Token 用量；不会发送 QQ。',async()=>{busy=true;try{const r=await api('long-memory',{action:'consolidate',chat:$('long-memory-chat').value});toast('已检查 '+(r.sources||0)+' 条原话，保存 '+r.saved+' 条压缩记忆');await load();}finally{busy=false;$('long-memory-consolidate').disabled=false;}});
  $('long-memory-reset').onclick=()=>window.Operations.confirm('重置这个 QQ 的磨合？','清除此 QQ 跨会话积累的表达倾向和兴趣观察，不改角色卡、聊天记录或事实记忆。',async()=>{await api('long-memory',{action:'reset-adaptation',chat:$('long-memory-chat').value});await load();});
  setInterval(()=>{if(state&&document.visibilityState==='visible'&&!$('page-memory').hidden&&!busy)load().catch(()=>{});},30000);
  return {sync,load};
})();
window.TokenUsage=(()=>{
  const num=x=>Number(x||0).toLocaleString('zh-CN');
  async function load(){
    const selected=$('usage-chat').value;
    const data=await api('token-usage?days='+$('usage-range').value+(selected==='*'?'':'&chat='+encodeURIComponent(selected))),t=data.total;
    const selector=$('usage-chat');selector.replaceChildren(new Option('全部会话','*'),new Option('历史未归属 / 系统调用',''));
    const chats=new Map(Object.entries(state?.config.conversations||{}).map(([key,c])=>[key,c.name||key]));
    data.conversations.filter(r=>r.chat).forEach(r=>chats.set(r.chat,r.name));
    chats.forEach((name,key)=>selector.add(new Option(name+' · '+key,key)));selector.value=selected;
    const body=$('usage-conversations');body.replaceChildren();
    data.conversations.filter(r=>selected==='*'||r.chat===selected).forEach(r=>{
      const row=document.createElement('tr');
      [r.name,r.calls,r.input_tokens,r.output_tokens,r.input_tokens+r.output_tokens].forEach((v,i)=>{const cell=document.createElement('td');cell.textContent=i?num(v):v;row.append(cell);});body.append(row);
    });
    if(!body.children.length){const row=document.createElement('tr'),cell=document.createElement('td');cell.colSpan=5;cell.textContent='所选时段暂无记录';row.append(cell);body.append(row);}
    $('usage-input').textContent=num(t.input_tokens);$('usage-output').textContent=num(t.output_tokens);
    $('usage-total').textContent=num(t.input_tokens+t.output_tokens);
    $('usage-cache').textContent=t.cache_reported?num(t.cache_hit):'未提供';
    $('usage-note').textContent=`${num(t.calls)} 次请求 · ${num(t.reported)} 次返回 Token 统计 · 缓存字段覆盖 ${num(t.cache_reported)} 次。北京时间；本机服务记录，非 DeepSeek 账户总账，不估算费用。`;
    const chart=$('usage-chart');chart.replaceChildren();const maximum=Math.max(1,...data.daily.map(x=>x.input_tokens+x.output_tokens));
    data.daily.forEach(d=>{const row=document.createElement('div');row.className='usage-day';const label=document.createElement('span');label.textContent=d.day.slice(5);const bar=document.createElement('meter');bar.min=0;bar.max=maximum;bar.value=d.input_tokens+d.output_tokens;bar.setAttribute('aria-label',d.day+' Token');const value=document.createElement('span');value.textContent=num(bar.value);row.append(label,bar,value);chart.append(row);});
    $('usage-breakdown').textContent=(t.cache_reported?`缓存未命中 ${num(t.cache_miss)} · 已报告输入中的命中率 ${Math.round(t.cache_hit/Math.max(1,t.cache_hit+t.cache_miss)*100)}%。 `:'')+data.models.map(m=>`${m.model}：输入 ${num(m.input_tokens)} / 输出 ${num(m.output_tokens)}`).join('；')+'。用途：'+data.purposes.map(p=>`${({chat:'聊天 / 连接测试',admin:'管理解析',sandbox:'试聊',memory:'记忆整理',legacy:'历史记录'}[p.purpose]||p.purpose)} ${num(p.input_tokens+p.output_tokens)}`).join('；');
  }
  $('usage-chat').onchange=action(load);$('usage-range').onchange=action(load);$('usage-refresh').onclick=action(load);
  return {load};
})();
