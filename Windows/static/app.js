"use strict";
const $ = (id) => document.getElementById(id);
let state = null;
let shutdownInProgress = false;
let toastTimer;
const labels = {overview:"概览",conversations:"会话与主动聊天",personality:"角色工坊",settings:"账号与连接",operations:"运行与数据",tools:"模型工具",memory:"记忆与磨合"};
const auditLabels = {reply_sent:"已回复",reply_superseded:"待发回复已更新",proactive_sent:"主动联系",skip:"保持安静",error:"需要检查",ui_change:"界面设置",admin_change:"QQ 管理指令",memory_cleared:"记忆已清除",model_test:"模型测试",queue_full:"消息繁忙",backup_created:"备份完成",backup_restored:"备份恢复",backup_error:"备份异常",runtime_job:"环境任务",memory_edited:"记忆已编辑",server_error:"管理接口异常",autonomy:"自主时间"};

function toast(message, error=false) {
  $("toast").textContent = message;
  $("toast").classList.toggle("error",error);
  $("toast").hidden=false;
  clearTimeout(toastTimer);
  toastTimer=setTimeout(()=>$("toast").hidden=true,error?7000:3500);
}
async function api(path, body) {
  const options={headers:{"X-Bot-Request":"1"},credentials:"same-origin"};
  if(body!==undefined){options.method="POST";options.headers["Content-Type"]="application/json";options.body=JSON.stringify(body);}
  const controller=new AbortController();options.signal=controller.signal;
  const timeout=setTimeout(()=>controller.abort(),path.startsWith('roleplay/test')||path==='test-model'?190000:path==='long-memory'?190000:path==='tools/test'?60000:30000);
  let response;
  try{response=await fetch("/api/"+path,options);}catch(error){throw new Error(error.name==='AbortError'?'请求超时，请检查运行记录后再操作；修改操作不会自动重试。':'无法连接本机管理服务，请检查服务是否运行。');}finally{clearTimeout(timeout);}
  const text=await response.text();
  let data;
  try{data=JSON.parse(text);}catch{data={error:text};}
  if(!response.ok){
    if(response.status===401){$("login").hidden=false;$("shell").hidden=true;}
    throw new Error((data.error||"请求失败")+(response.status>=500&&data.request_id?"（记录编号："+data.request_id+"）":""));
  }
  return data;
}
function action(handler) {
  return async (event)=>{
    event?.preventDefault();
    const button=event?.submitter||((event?.currentTarget?.tagName==="BUTTON")?event.currentTarget:null);
    if(button)button.disabled=true;
    try{await handler(event);}catch(error){toast(error.message,true);}
    finally{if(button)button.disabled=false;}
  };
}
function page(name){
  if(!labels[name])return;
  sessionStorage.setItem("qqbot-page",name);
  document.querySelectorAll(".page").forEach(el=>el.hidden=el.id!=="page-"+name);
  document.querySelectorAll(".nav").forEach(el=>el.classList.toggle("active",el.dataset.page===name));
  $("breadcrumb").textContent="控制台 / "+labels[name];
  window.scrollTo({top:0,behavior:"instant"});
  if(name==="tools"){window.ToolMenu?.load().catch(error=>toast(error.message,true));window.LifeUI?.load().catch(error=>toast(error.message,true));}
  if(name==='memory')window.LongMemory?.load().catch(error=>toast(error.message,true));
  if(name==='overview')window.TokenUsage?.load().catch(error=>toast(error.message,true));
}
document.querySelectorAll(".nav").forEach(el=>el.onclick=()=>page(el.dataset.page));
document.querySelectorAll("[data-go]").forEach(el=>el.onclick=()=>page(el.dataset.go));

function updateStatus(data){
  window.LongMemory?.sync(data);
  window.latestStatus=data;
  const cfg=data.config;
  const conversations=Object.values(cfg.conversations);
  $("stat-allowed").textContent=conversations.filter(c=>c.allowed).length;
  $("stat-proactive").textContent=conversations.filter(c=>c.allowed&&c.proactive).length;
  $("stat-calls").textContent=data.today_calls;
  $("stat-budget").textContent="每日上限 "+cfg.daily_model_limit+" 次 · 含判断与管理";
  $("run-label").textContent=cfg.paused?"机器人已暂停":"等待恰好的回应";
  $("run-indicator").classList.toggle("paused",cfg.paused);
  $("pause").textContent=cfg.paused?"恢复机器人":"暂停机器人";
  $("bridge-pill").textContent=data.offline?"离线检查模式":data.bridge.online?"QQ 在线":data.bridge.connected?"消息通道已连，正在确认账号状态":"QQ 未连接";
  $("bridge-pill").classList.toggle("online",data.bridge.online);
  $("bridge-detail").textContent=data.bridge.online?"已登录 QQ："+data.bridge.self_id:(data.bridge.error||"消息通道已连接，正在确认 QQ 在线状态");
  $("setup-card").hidden=!!(cfg.admin_qq.length&&cfg.bot_qq&&cfg.has_api_key&&data.bridge.online);
  const audit=$("audit");audit.replaceChildren();
  if(!data.audit.length){const p=document.createElement("p");p.className="empty";p.textContent="还很安静。连接账号、授权会话后，活动会出现在这里。";audit.append(p);}
  data.audit.slice(0,8).forEach(row=>{
    const el=document.createElement("div");el.className="audit-row";
    const time=document.createElement("time");time.textContent=new Date(row.time*1000).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"});
    const kind=document.createElement("span");kind.textContent=auditLabels[row.kind]||row.kind;
    const detail=document.createElement("span");detail.textContent=row.detail;
    el.append(time,kind,detail);audit.append(el);
  });
  document.querySelectorAll('.conversation-card').forEach(form=>{
    const key=form.dataset.key,schedule=data.states[key],conv=data.config.conversations[key];
    if(!schedule||!conv)return;
    const label=form.querySelector('.next-check');if(!label)return;
    label.textContent=conv.proactive&&schedule.next_at?'下次主动检查：'+new Date(schedule.next_at*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'}):'主动聊天尚未安排';
    if(schedule.reply_at)label.textContent+=' · 正在等合适时机回复，预计 '+new Date(schedule.reply_at*1000).toLocaleTimeString('zh-CN');
  });
  window.Operations?.update(data);
  if(!$("page-tools").hidden){window.ToolMenu?.load().catch(()=>{});window.LifeUI?.load().catch(()=>{});}
}
function fillForms(){
  const cfg=state.config;
  if($("settings-form").dataset.dirty!=="true"){
  $("settings-form").dataset.revision=cfg.revision;
  $("admin-qq").value=cfg.admin_qq.join(", ");
  $("bot-qq").value=cfg.bot_qq;
  $("api-key").value="";$("api-key").placeholder=cfg.has_api_key?"已保存 · 留空保留现有密钥":"尚未设置，请填入 API Key";
  $("model").value=cfg.model;
  $("daily-model-limit").value=cfg.daily_model_limit;
  }
  if($("persona-form").dataset.dirty!=="true"){
  $("persona-form").dataset.revision=cfg.revision;
  $("persona").value=cfg.persona;
  $("history-messages").value=cfg.history_messages;
  }
  renderConversations();
  const selected=$("memory-chat").value;
  $("memory-chat").replaceChildren(new Option("请选择一个会话",""));
  Object.entries(cfg.conversations).forEach(([key,conv])=>$("memory-chat").add(new Option((conv.name||key)+" · "+key,key)));
  if(cfg.conversations[selected])$("memory-chat").value=selected;
  window.Studio?.sync(cfg.roleplay);
  window.ToolMenu?.sync(cfg.tools,cfg.revision);
  window.LifeUI?.sync(cfg.life,cfg.revision);
}
async function refresh(forms=true){
  const data=await api(forms?"status":"status?light=1");
  if(forms){state=data;fillForms();}
  updateStatus(data);
  if(forms)window.TokenUsage?.load().catch(error=>toast(error.message,true));
  $('connection-banner').hidden=true;
  if(!forms&&state&&data.config.revision!==state.config.revision){$('connection-banner').textContent='设置已被其他操作修改。请先保留未保存的内容，再点击右上角刷新；过期设置会被拒绝覆盖。';$('connection-banner').hidden=false;}
  $("login").hidden=true;$("shell").hidden=false;
}
async function save(patch, revision=state?.config.revision){
  if(!state)throw new Error("请先加载设置");
  await api("settings",{revision,patch});
  if(patch.conversations){Object.keys(patch.conversations).forEach(key=>{const form=[...document.querySelectorAll('.conversation-card')].find(el=>el.dataset.key===key);if(form)form.dataset.dirty='false';});}
  if(['admin_qq','bot_qq','api_key','model','daily_model_limit'].some(key=>key in patch))$('settings-form').dataset.dirty='false';
  if('persona' in patch||'history_messages' in patch)$('persona-form').dataset.dirty='false';
  if(patch.tools)window.ToolMenu?.saved();
  if(patch.life)window.LifeUI?.saved();
  await refresh();
  toast("设置已保存");
}
$("refresh").onclick=action(()=>refresh());
$("login-form").onsubmit=action(async()=>{await api("login",{token:$("login-token").value.trim()});$("login-token").value="";await refresh();});
$("logout").onclick=action(async()=>{await api("logout",{});state=null;$("login").hidden=false;$("shell").hidden=true;});
$("pause").onclick=action(()=>save({paused:!(window.latestStatus||state).config.paused}));
function connectionPatch(){
  const patch={admin_qq:$("admin-qq").value.split(/[,，\s]+/).filter(Boolean),bot_qq:$("bot-qq").value.trim(),model:$("model").value.trim(),daily_model_limit:Number($("daily-model-limit").value)};
  if($("api-key").value.trim())patch.api_key=$("api-key").value.trim();
  return patch;
}
$("settings-form").onsubmit=action(async()=>{await save(connectionPatch(),Number($("settings-form").dataset.revision));$("open-bridge").textContent="打开 QQ 扫码登录";});
$("settings-form").addEventListener('input',()=>$("open-bridge").textContent="保存并扫码登录");
$("persona-form").onsubmit=action(()=>save({persona:$("persona").value,history_messages:Number($("history-messages").value)},Number($("persona-form").dataset.revision)));
$('settings-reload').onclick=()=>window.Operations.confirm('重新载入账号设置？','未保存的账号与连接设置会被放弃，读取最新设置。',async()=>{$('settings-form').dataset.dirty='false';await refresh();});
$("test-model").onclick=action(async()=>{if($('settings-form').dataset.dirty==='true')throw new Error('请先保存账号与连接设置，再测试连接。');toast("正在测试 DeepSeek…");await api("test-model",{});toast("DeepSeek 已连接，结构化响应检查通过");await refresh(false);});
$("open-bridge").onclick=action(async()=>{
  if($("settings-form").dataset.dirty==='true'){
    if(!$("settings-form").reportValidity())return;
    await save(connectionPatch(),Number($("settings-form").dataset.revision));
    $("open-bridge").textContent="打开 QQ 扫码登录";
  }
  window.QQLogin?.open();
});

async function addChat(kind,id,name){
  if(!/^[1-9][0-9]{4,14}$/.test(id))throw new Error("请输入有效的群号或 QQ 号");
  const key=kind+":"+id;
  if(state.config.conversations[key])throw new Error("该会话已添加，请在下面修改");
  $("conversation-search").value="";$("conversation-filter").value="all";
  await save({conversations:{[key]:{name,allowed:false,proactive:false}}});
  const added=[...document.querySelectorAll('.conversation-card')].find(form=>form.dataset.key===key);
  added?.querySelector('.conversation-toggle')?.click();added?.scrollIntoView({block:'start',behavior:'smooth'});
  toast("会话已添加；开启授权并保存后才会响应");
}
$("add-chat").onsubmit=action(async()=>{await addChat($("new-kind").value,$("new-id").value.trim(),$("new-name").value.trim());$("new-id").value="";$("new-name").value="";});
$("load-contacts").onclick=action(async()=>{
  if(!window.latestStatus?.bridge.online){toast("请先扫码登录，再读取群与好友。");window.QQLogin?.open();return;}
  const contacts=await api("contacts");const box=$("contacts");box.replaceChildren();box.hidden=false;
  [["group",contacts.groups],["private",contacts.friends]].forEach(([kind,rows])=>rows.forEach(row=>{
    const button=document.createElement("button");button.type="button";button.textContent=(kind==="group"?"群聊 · ":"私聊 · ")+row.name+" / "+row.id+" ＋";
    button.onclick=action(async()=>{await addChat(kind,row.id,row.name);box.hidden=true;});box.append(button);
  }));
  if(!box.children.length)box.textContent="未读取到群或好友，请先登录 QQ。";
});
function renderConversations(){
  const list=$("conversation-list");
  const preserved=new Map([...list.querySelectorAll('.conversation-card')].filter(el=>el.dataset.dirty==='true').map(el=>[el.dataset.key,el]));
  list.replaceChildren();
  const entries=Object.entries(state.config.conversations);
  if(!entries.length){const empty=document.createElement("div");empty.className="empty";empty.textContent="还没有授权任何会话。从上方添加一个群或好友开始。";list.append(empty);}
  entries.forEach(([key,conv])=>{
    if(preserved.has(key)){list.append(preserved.get(key));return;}
    const form=document.createElement("form");form.className="card conversation-card";form.dataset.key=key;form.dataset.revision=state.config.revision;form.dataset.allowed=String(conv.allowed);form.dataset.proactive=String(conv.proactive);
    // Only static markup is parsed; all account IDs, names and model text use textContent/value.
    form.innerHTML=`<div class="conversation-header"><div><h2 class="chat-title"></h2><small class="chat-key"></small></div><span class="pill chat-status"></span></div>
      <label>会话备注<input name="name" maxlength="100"></label>
      <div class="check-row"><label class="checkbox-label"><input name="allowed" type="checkbox">允许读取并回复</label><label class="checkbox-label"><input name="proactive" type="checkbox">开启主动聊天</label></div>
      <div class="check-row"><label class="checkbox-label"><input name="tools_enabled" type="checkbox">允许此会话使用工具</label></div><label class="chat-role-worlds">仅允许这些工具（不选则跟随全局；会话和全局必须同时启用）<select name="tool_allowlist" multiple><option value="web_search">网页搜索</option><option value="web_read">读取网页</option><option value="calculator">计算器</option><option value="current_time">日期与时间</option><option value="life_overview">生活与可用素材</option><option value="inspect_images">看图片</option><option value="sticker_find">挑选表情包</option><option value="sticker_save">收藏喜欢的表情</option><option value="sticker_search">搜索表情包</option><option value="qq_send">发消息或表情</option><option value="qq_poke">拍一拍 / 戳一戳</option><option value="qq_like">资料卡点赞</option><option value="qq_react">消息表情回应</option><option value="qq_recall">撤回自己的消息</option><option value="qzone_read">逛QQ空间</option><option value="qq_context">回想一个会话</option><option value="qq_contacts">查看QQ联系人</option><option value="qzone_publish">发表自己的说说</option><option value="qzone_like">给说说点赞</option><option value="qzone_comment">评论已授权对象的说说</option></select></label>
      <div class="chat-role-bindings"><label>角色卡<select name="character_id"></select></label><label>用户人设<select name="user_persona_id"></select></label><label>生成预设<select name="preset_id"></select></label></div>
      <label class="chat-role-worlds">额外世界书（可多选；在角色自带和全局世界书之外叠加）<select name="worldbook_ids" multiple></select></label>
      <div class="schedule-grid"><label>最短间隔 / 分钟<input name="min_minutes" type="number" min="1" max="10080" required></label><label>最长间隔 / 分钟<input name="max_minutes" type="number" min="1" max="10080" required></label><label>允许开始<input name="start" type="time" required></label><label>允许结束<input name="end" type="time" required></label><label>每日主动消息上限<input name="daily_limit" type="number" min="1" max="100" required></label></div>
      <p class="schedule-note">北京时间。相同起止时间表示全天；支持跨午夜。模型在间隔范围内决定下次检查时间，也可以选择不发消息；最长间隔不保证一定发出消息。断网或休眠期间不补发。</p>
      <div class="form-row"><label class="checkbox-label"><input name="reply_timing" type="checkbox">让模型决定回复节奏</label><label>首条目标等待上限 / 秒<input name="max_reply_delay" type="number" min="2" max="120" required></label></div><p class="schedule-note">首条等待计入模型处理耗时，处理更久则完成后尽快发送；后续气泡由模型安排1～12秒间隔。你继续发消息时会取消过时的待发内容，结合新消息重新判断；管理指令不额外延迟。等待不跨重启补发。</p>
      <details><summary>为这个会话单独设置性格</summary><textarea name="persona" rows="4" maxlength="12000" placeholder="留空使用全局性格"></textarea></details>
      <p class="schedule-note next-check"></p><div class="actions"><button type="submit">保存会话设置</button><button type="button" class="secondary see-memory">查看记忆 ↗</button><button type="button" class="text-button revoke">撤销授权</button></div>`;
    const header=form.querySelector('.conversation-header');
    const body=document.createElement('div');body.className='conversation-body';body.hidden=true;
    [...form.children].filter(child=>child!==header).forEach(child=>body.append(child));form.append(body);
    const toggle=document.createElement('button');toggle.type='button';toggle.className='conversation-toggle';toggle.textContent='编辑设置';toggle.setAttribute('aria-expanded','false');toggle.onclick=()=>{body.hidden=!body.hidden;toggle.textContent=body.hidden?'编辑设置':'收起设置';toggle.setAttribute('aria-expanded',String(!body.hidden));};header.append(toggle);
    const meta=document.createElement('span');meta.className='conversation-meta';const actor=state.config.roleplay?.characters.find(c=>c.id===(state.config.life?.enabled?state.config.roleplay.active_character:(conv.character_id||state.config.roleplay.active_character)));meta.textContent=(actor?.name||'默认角色')+' · '+(conv.proactive?`主动检查 ${conv.min_minutes}～${conv.max_minutes} 分钟 / ${conv.start}—${conv.end}`:'仅回应消息');header.firstElementChild.append(meta);
    form.querySelector(".chat-title").textContent=conv.name||(key.startsWith("group:")?"群聊":"私聊");
    form.querySelector(".chat-key").textContent=key;
    form.querySelector(".chat-status").textContent=conv.allowed?"已授权":"未授权";
    const pack=state.config.roleplay;
    if(pack){
      for(const [field,items] of [["character_id",pack.characters],["user_persona_id",pack.personas],["preset_id",pack.presets]]){
        const select=form.elements.namedItem(field);select.add(new Option("跟随工坊默认",""));items.forEach(item=>select.add(new Option(item.name,item.id)));
      }
      pack.worldbooks.forEach(book=>form.elements.worldbook_ids.add(new Option(book.name,book.id)));
    }
    Object.entries(conv).forEach(([name,value])=>{const input=form.elements.namedItem(name);if(input){if(input.type==="checkbox")input.checked=value;else if(input.multiple)[...input.options].forEach(option=>option.selected=value.includes(option.value));else input.value=value;}});
    if(state.config.life?.enabled){const selected=form.elements.character_id;selected.value='';selected.disabled=true;selected.title='单 bot 模式：使用角色工坊当前启用的人格';selected.options[0].textContent='跟随账号统一人格';}
    const schedule=state.states[key];
    form.querySelector(".next-check").textContent=conv.proactive&&schedule?.next_at?"下次计划检查："+new Date(schedule.next_at*1000).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai"}):"主动聊天尚未安排";
    if(schedule?.reply_at)form.querySelector(".next-check").textContent+=" · 回复预计发送："+new Date(schedule.reply_at*1000).toLocaleTimeString("zh-CN");
    form.elements.allowed.onchange=()=>{if(!form.elements.allowed.checked)form.elements.proactive.checked=false;};
    form.elements.proactive.onchange=()=>{if(form.elements.proactive.checked)form.elements.allowed.checked=true;};
    form.onsubmit=action(async()=>{
      const value={};
      Object.keys(conv).forEach(name=>{const input=form.elements.namedItem(name);if(!input)return;value[name]=input.multiple?[...input.selectedOptions].map(option=>option.value):input.type==="checkbox"?input.checked:input.type==="number"?Number(input.value):input.value;});
      await save({conversations:{[key]:value}},Number(form.dataset.revision));
    });
    form.querySelector(".see-memory").onclick=action(async()=>{page("personality");window.Studio?.show("memory");$("memory-chat").value=key;await loadMemory();});
    form.querySelector(".revoke").onclick=action(()=>save({conversations:{[key]:{allowed:false,proactive:false}}}));
    const discard=document.createElement('button');discard.type='button';discard.className='text-button';discard.textContent='重新载入 / 放弃草稿';discard.onclick=()=>window.Operations.confirm('重新载入会话设置？','该会话未保存的编辑会被放弃，重新读取最新设置。',async()=>{form.dataset.dirty='false';await refresh();});form.querySelector('.actions').append(discard);
    list.append(form);
  });
  filterConversations();
}
function filterConversations(){
  const query=$('conversation-search').value.trim().toLocaleLowerCase(),filter=$('conversation-filter').value;
  let count=0;document.querySelectorAll('.conversation-card').forEach(form=>{const key=form.dataset.key;const conv=state.config.conversations[key];const matches=(key+' '+conv.name).toLocaleLowerCase().includes(query)&&(filter==='all'||filter==='allowed'&&conv.allowed||filter==='blocked'&&!conv.allowed||filter==='proactive'&&conv.proactive||key.startsWith(filter+':'));form.hidden=!matches;if(matches)count++;});
  $('conversation-count').textContent=count+' / '+Object.keys(state.config.conversations).length+' 个会话';$('conversation-no-results').hidden=count!==0||!Object.keys(state.config.conversations).length;
}
$('conversation-search').oninput=filterConversations;$('conversation-filter').onchange=filterConversations;
let memoryBefore=0, memoryOriginal='', memoryEditPrevious='';
async function loadMemory(more=false){
  const key=$("memory-chat").value;
  $("clear-memory").disabled=!key;
  if(!more)$("memory-messages").replaceChildren();
  if(!key){$("memory-more").hidden=true;$("memory-export").hidden=true;$("memory-edit").disabled=true;$("memory-summary").textContent="选择一个会话，查看长期摘要与最近消息。";return;}
  const memoryQuery=$('memory-query').value.trim();
  const data=await api("history?page=1&chat="+encodeURIComponent(key)+'&q='+encodeURIComponent(memoryQuery)+'&before='+(more?memoryBefore:0));
  if($("memory-chat").value!==key||$('memory-query').value.trim()!==memoryQuery)return;
  memoryBefore=data.before;memoryOriginal=data.memory;
  $('memory-more').hidden=!data.more;$('memory-export').href='/api/memory/export?chat='+encodeURIComponent(key);$('memory-export').hidden=false;$('memory-edit').disabled=false;
  if($("memory-chat").value!==key)return;
  $("memory-summary").textContent=data.memory||"这个会话还没有长期记忆摘要。";
  const fragment=document.createDocumentFragment();
  if(!more&&!data.messages.length){const empty=document.createElement("p");empty.className="empty";empty.textContent=memoryQuery?"没有匹配的消息，试试其他关键词。":"还没有本地聊天记录。";fragment.append(empty);}
  data.messages.forEach(row=>{
    const article=document.createElement("article");article.className="message";
    const info=document.createElement("small");info.textContent=(row.role==="assistant"?"栖":"QQ "+row.sender)+" · "+new Date(row.time*1000).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai"});
    const text=document.createElement("p");text.textContent=row.text;article.append(info,text);fragment.append(article);
  });
  if(more)$("memory-messages").prepend(fragment);else $("memory-messages").append(fragment);
}
$("memory-chat").onchange=action(()=>{$("memory-query").value="";$("memory-editor").hidden=true;return loadMemory();});
const memoryToolbar=document.createElement('div');memoryToolbar.className='memory-toolbar';memoryToolbar.innerHTML='<label>搜索历史<input id="memory-query" type="search" maxlength="200" placeholder="搜索当前会话的消息正文"></label><button id="memory-search" class="secondary">搜索</button><a id="memory-export" class="button-link secondary" download hidden>导出记忆</a><button id="memory-edit" class="text-button" disabled>编辑长期摘要</button>';
$('memory-messages').before(memoryToolbar);
const memoryEditor=document.createElement('div');memoryEditor.id='memory-editor';memoryEditor.className='memory-editor';memoryEditor.hidden=true;memoryEditor.innerHTML='<label>长期摘要 / 最多 2000 字<textarea id="memory-edit-text" rows="6" maxlength="2000"></textarea></label><div class="actions"><button id="memory-edit-save">保存摘要</button><button id="memory-edit-cancel" class="secondary">取消</button></div>';
memoryToolbar.after(memoryEditor);
const memoryMore=document.createElement('button');memoryMore.id='memory-more';memoryMore.className='text-button';memoryMore.textContent='加载更早消息';memoryMore.hidden=true;$('memory-messages').after(memoryMore);
$('memory-search').onclick=action(()=>loadMemory());$('memory-query').onkeydown=event=>{if(event.key==='Enter')$('memory-search').click();};$('memory-more').onclick=action(()=>loadMemory(true));
$('memory-edit').onclick=()=>{memoryEditPrevious=memoryOriginal;$('memory-edit-text').value=memoryOriginal;$('memory-editor').hidden=false;};$('memory-edit-cancel').onclick=()=>$('memory-editor').hidden=true;
$('memory-edit-save').onclick=action(async()=>{await api('memory',{chat:$('memory-chat').value,memory:$('memory-edit-text').value,previous:memoryEditPrevious});$('memory-editor').hidden=true;await loadMemory();toast('长期摘要已更新');});
for(const eventName of ['input','change'])document.addEventListener(eventName,event=>{const form=event.target.closest('#settings-form,#persona-form,.conversation-card');if(form){form.dataset.dirty='true';}});
window.addEventListener('beforeunload',event=>{if(document.querySelector('form[data-dirty="true"]')||(!$('memory-editor').hidden&&$('memory-edit-text').value!==memoryEditPrevious)){event.preventDefault();event.returnValue='';}});
$("clear-memory").onclick=()=>$("confirm-dialog").showModal();
$("cancel-clear").onclick=()=>$("confirm-dialog").close();
$("confirm-clear").onclick=action(async()=>{await api("clear-memory",{chat:$("memory-chat").value});$("confirm-dialog").close();await loadMemory();toast("会话记忆已清除");});

(async()=>{
  const token=new URLSearchParams(location.hash.slice(1)).get("token");
  if(location.hash)history.replaceState(null,"",location.pathname);
  try{if(token)await api("login",{token});await refresh();page(sessionStorage.getItem("qqbot-page")||"overview");}catch(error){if(token)toast(error.message,true);}
})();
setInterval(async()=>{if(!state||document.hidden||shutdownInProgress)return;try{await refresh(false);}catch(error){$('connection-banner').textContent='状态更新失败：'+error.message;$('connection-banner').hidden=false;$('bridge-pill').textContent='状态已过期';}},15000);
