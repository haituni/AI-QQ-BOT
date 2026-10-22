"use strict";
// All imported text is rendered as text/value. No HTML, JavaScript or remote assets are executed.
window.Studio = (() => {
  let draft=null, saved=null, dirty=false, currentTab="characters";
  let selected={characters:"",personas:"",worldbooks:"",presets:"",entry:""};
  let testHistory=[], testEmotion=null, testEpoch=0, busy=false;
  const names={main:"主提示词",persona:"用户人设",lore_before:"角色前世界书",character:"角色定义",scenario:"场景",lore_after:"角色后世界书",examples:"示例对话",memory:"会话长期记忆",history:"聊天历史 / 深度注入",post:"后置指令"};
  const tabs=["characters","personas","books","presets","sandbox","memory"];
  const clone=(value)=>JSON.parse(JSON.stringify(value));
  const uid=(prefix)=>prefix+"_"+crypto.randomUUID().replaceAll("-","").slice(0,16);
  const split=(text)=>text.split(/[,，\n]+/).map(v=>v.trim()).filter(Boolean);
  const get=(collection,id=selected[collection])=>draft?.[collection].find(x=>x.id===id);
  const dom=(tag,text,className)=>{const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(className)el.className=className;return el;};
  const listen=(id,handler)=>$(id).addEventListener("click",action(handler));
  const formValues=(form,item)=>Object.entries(item).forEach(([key,value])=>{
    const el=form.elements.namedItem(key);if(!el)return;
    if(el.type==="checkbox")el.checked=value;
    else if(key==="alternate_greetings")el.value=value.join("\n---\n");
    else if(Array.isArray(value))el.value=value.join(", ");
    else el.value=value;
  });
  function mark(){dirty=true;$("studio-dirty").textContent="草稿 · 未保存";$("studio-dirty").classList.add("is-dirty");}
  function clean(){dirty=false;$("studio-dirty").textContent="已保存";$("studio-dirty").classList.remove("is-dirty");}
  function options(el,items,value,empty){
    el.replaceChildren();if(empty!==undefined)el.add(new Option(empty,""));
    items.forEach(item=>el.add(new Option(item.name,item.id)));
    if([...el.options].some(option=>option.value===value))el.value=value;
  }
  function renderSelectors(){
    if(!draft)return;
    for(const [kind,suffix,field] of [["characters","character","active_character"],["personas","persona","active_persona"],["presets","preset","active_preset"]]){
      options($("studio-default-"+suffix),draft[kind],draft[field]);
      const el=$("studio-test-"+suffix);options(el,draft[kind],el.value||draft[field]);
    }
    const chat=$("studio-test-chat"), value=chat.value;
    options(chat,Object.entries(state?.config.conversations||{}).map(([id,c])=>({id,name:(c.name||id)+" · "+id})),value,"独立试聊");
    $("studio-test-memory").disabled=!chat.value;$("studio-preview-live").disabled=!chat.value;
  }
  function show(name){
    if(!tabs.includes(name))return;
    currentTab=name;
    tabs.forEach(tab=>$("studio-"+tab).hidden=tab!==name);
    document.querySelectorAll("[data-studio-tab]").forEach(el=>el.classList.toggle("selected",el.dataset.studioTab===name));
  }
  function portrait(el,card){
    el.replaceChildren();
    if(card.avatar){const image=dom("img");image.src=card.avatar;image.alt=card.name+"头像";el.append(image);}
    else el.append(dom("span",card.name.slice(0,1)||"◇"));
  }
  function library(collection,id,render,filter=""){
    const box=$(id);box.replaceChildren();
    draft[collection].filter(item=>(item.name+" "+(item.tags||[]).join(" ")+" "+(item.creator||"")).toLowerCase().includes(filter.toLowerCase())).forEach(item=>{
      const button=dom("button",undefined,"studio-library-item"+(selected[collection]===item.id?" selected":""));button.type="button";
      if(collection==="characters"){const avatar=dom("div",undefined,"studio-avatar-small");portrait(avatar,item);button.append(avatar);}
      const text=dom("div");text.append(dom("strong",item.name));
      const defaultId=draft[{characters:"active_character",personas:"active_persona",presets:"active_preset"}[collection]];
      const subtitle=collection==="characters"?(item.tags.slice(0,3).join(" / ")||"人物档案"):collection==="worldbooks"?item.entries.length+" 个条目":collection==="presets"?"提示词与生成参数":"{{user}} 的身份";
      text.append(dom("small",(item.id===defaultId?"默认 · ":"")+subtitle));button.append(text);
      button.onclick=()=>{selected[collection]=item.id;if(collection==="worldbooks")selected.entry="";render();};box.append(button);
    });
    if(!box.children.length)box.append(dom("p","没有匹配的项目。","studio-hint"));
  }
  function renderGreeting(){
    const card=get("characters"),el=$("studio-character-form").elements.greeting_index;
    el.replaceChildren(new Option("首条开场白",0));card.alternate_greetings.forEach((g,i)=>el.add(new Option("备选 "+(i+1)+" · "+g.slice(0,22),i+1)));
    card.greeting_index=Math.min(card.greeting_index,card.alternate_greetings.length);el.value=card.greeting_index;
  }
  function bookChecks(el,values,change){
    el.replaceChildren();draft.worldbooks.forEach(book=>{
      const label=dom("label",undefined,"checkbox-label"),input=dom("input");input.type="checkbox";input.checked=values.includes(book.id);
      input.onchange=()=>{change([...el.querySelectorAll("input:checked")].map(x=>x.value));mark();};input.value=book.id;
      label.append(input,document.createTextNode(book.name));el.append(label);
    });if(!draft.worldbooks.length)el.append(dom("span","先在世界书中添加设定，再来关联。","studio-hint"));
  }
  function characterMeta(){
    const card=get("characters");$("studio-character-heading").textContent=card.name||"未命名角色";
    const layered=card.extensions?.qq_companion?.prompt_layers?.enabled;
    $("studio-character-size").textContent=(layered?"完整原卡已保存，运行时分层检索 · ":"人物常驻定义约 ")+(card.name+card.description+card.personality+card.scenario).length.toLocaleString()+" 字符 · "+card.alternate_greetings.length+" 条备选开场";
    portrait($("studio-portrait"),card);
  }
  function renderCharacters(){
    if(!get("characters"))selected.characters=draft.active_character;
    library("characters","studio-character-list",renderCharacters,$("studio-search").value);
    const card=get("characters");renderGreeting();formValues($("studio-character-form"),card);characterMeta();
    renderLayerControls(card);
    bookChecks($("studio-character-books"),card.worldbook_ids,value=>card.worldbook_ids=value);
  }
  function renderLayerControls(card){
    const box=$("studio-context-layers");box.replaceChildren();
    const policy=card.extensions?.qq_companion?.prompt_layers;if(!policy)return;
    const fieldset=dom("fieldset");fieldset.append(dom("legend","分层上下文 · 原文完整保存"));
    fieldset.append(dom("p","下方原卡全文仍可编辑与导出；开启分层后，每轮发送这里的常驻核心，再检索相关原文。修改原文不会自动重写核心，请将需要始终生效的人设同步到核心。","studio-hint"));
    const toggle=dom("input");toggle.type="checkbox";toggle.checked=policy.enabled===true;
    const label=dom("label",undefined,"checkbox-label");label.append(toggle,document.createTextNode("启用分层上下文"));fieldset.append(label);
    toggle.oninput=()=>{policy.enabled=toggle.checked;mark();characterMeta();};
    const details=dom("details");details.append(dom("summary","编辑实际常驻人设与预算"));const fields=dom("div",undefined,"studio-form studio-advanced");
    for(const [key,title] of Object.entries({description:"常驻身份与形象",personality:"常驻性格与表达",scenario:"常驻场景和关系边界",mes_example:"常驻示例（可留空）"})){
      const label=dom("label",title),input=dom("textarea");input.rows=4;input.maxLength=3000;input.value=policy.core[key]||"";input.oninput=()=>{policy.core[key]=input.value;mark();};label.append(input);fields.append(label);
    }
    const grid=dom("div",undefined,"studio-field-grid");
    for(const [key,title,min,max] of [["detail_chars","检索原文 / 字符",0,4000],["history_messages","近期消息上限 / 条",2,40],["history_chars","近期历史 / 字符",500,12000],["memory_chars","摘要与记忆 / 字符",500,4000],["lore_chars","世界书 / 字符",500,6000]]){
      const label=dom("label",title),input=dom("input");input.type="number";input.min=min;input.max=max;input.step=1;input.value=policy[key];input.oninput=()=>{policy[key]=Number(input.value);mark();};label.append(input);grid.append(label);
    }
    fields.append(grid,dom("p","预算按字符计算，并非精确Token。最新一条用户消息不会被局部历史预算切断；长期记忆仍按用户及群聊可见范围检索。","studio-hint"));details.append(fields);fieldset.append(details);box.append(fieldset);
  }
  function renderPersonas(){
    if(!get("personas"))selected.personas=draft.active_persona;
    library("personas","studio-persona-list",renderPersonas);formValues($("studio-persona-form"),get("personas"));
  }
  function renderBooks(){
    if(!get("worldbooks"))selected.worldbooks=draft.worldbooks[0]?.id||"";
    library("worldbooks","studio-book-list",renderBooks);
    const book=get("worldbooks");$("studio-book-empty").hidden=!!book;$("studio-book-editor").hidden=!book;
    if(!book)return;
    $("studio-book-name").value=book.name;$("studio-book-global").checked=draft.global_worldbook_ids.includes(book.id);
    if(!book.entries.some(e=>e.id===selected.entry))selected.entry=book.entries[0]?.id||"";
    const list=$("studio-entry-list");list.replaceChildren();
    book.entries.forEach(item=>{
      const button=dom("button",undefined,"studio-entry"+(item.id===selected.entry?" selected":""));button.type="button";
      button.append(dom("span",item.enabled?(item.constant?"◆":"◇"):"○"),dom("strong",item.name),dom("small","优先级 "+item.priority));
      button.onclick=()=>{selected.entry=item.id;renderBooks();};list.append(button);
    });
    const item=book.entries.find(e=>e.id===selected.entry);$("studio-entry-form").hidden=!item;
    if(item)formValues($("studio-entry-form"),item);
  }
  function renderOrder(){
    const p=get("presets"),box=$("studio-prompt-order");box.replaceChildren();
    p.sections.forEach((section,index)=>{
      const row=dom("div",undefined,"studio-order-row"),label=dom("label",undefined,"checkbox-label"),check=dom("input");check.type="checkbox";check.checked=section.enabled;check.disabled=section.id==="history";
      check.onchange=()=>{section.enabled=check.checked;mark();};label.append(check,document.createTextNode(String(index+1).padStart(2,"0")+"  "+names[section.id]));
      const up=dom("button","↑","studio-order-button"),down=dom("button","↓","studio-order-button");up.type=down.type="button";
      up.setAttribute("aria-label","上移"+names[section.id]);down.setAttribute("aria-label","下移"+names[section.id]);up.disabled=index===0;down.disabled=index===p.sections.length-1;
      const move=(delta)=>{[p.sections[index],p.sections[index+delta]]=[p.sections[index+delta],p.sections[index]];mark();renderOrder();};up.onclick=()=>move(-1);down.onclick=()=>move(1);
      row.append(label,up,down);box.append(row);
    });box.append(dom("div","固定末尾 · 运行协议与本轮触发","studio-order-fixed"));
  }
  function renderPresets(){
    if(!get("presets"))selected.presets=draft.active_preset;
    library("presets","studio-preset-list",renderPresets);formValues($("studio-preset-form"),get("presets"));renderOrder();
  }
  function renderAll(){renderSelectors();renderCharacters();renderPersonas();renderBooks();renderPresets();show(currentTab);}
  function sync(pack){
    if(!pack)return;
    if(dirty&&JSON.stringify(pack)!==JSON.stringify(draft)){
      if(saved&&JSON.stringify(pack)!==JSON.stringify(saved))toast("角色库已在其他操作中变更；当前草稿仍保留，请先导出草稿或撤销修改。",true);
      renderSelectors();return;
    }
    const first=!draft;
    draft=clone(pack);saved=clone(pack);clean();renderAll();if(first)resetTest();
  }
  function bindEditor(formId,collection,after){
    const form=$(formId);form.onsubmit=event=>event.preventDefault();
    form.addEventListener("input",event=>{
      const el=event.target,key=el.name;if(!key)return;
      const item=collection==="entry"?get("worldbooks")?.entries.find(e=>e.id===selected.entry):get(collection);
      if(!item||!(key in item))return;
      if(el.type==="checkbox")item[key]=el.checked;
      else if(el.type==="number"||key==="greeting_index")item[key]=Number(el.value);
      else if(key==="alternate_greetings")item[key]=el.value.split(/^\s*---\s*$/m).map(x=>x.trim()).filter(Boolean);
      else if(Array.isArray(item[key]))item[key]=split(el.value);
      else item[key]=el.value;
      mark();after?.(key,item);
    });
    // Native selects dispatch change reliably even when input isn't available in older WebKit.
    form.addEventListener("change",event=>{if(event.target.tagName==="SELECT")event.target.dispatchEvent(new Event("input",{bubbles:true}));});
  }
  bindEditor("studio-character-form","characters",(key)=>{
    if(key==="alternate_greetings")renderGreeting();
    if(["name","tags","creator"].includes(key)){library("characters","studio-character-list",renderCharacters,$("studio-search").value);renderSelectors();}
    characterMeta();
  });
  bindEditor("studio-persona-form","personas",key=>{if(key==="name"){library("personas","studio-persona-list",renderPersonas);renderSelectors();}});
  bindEditor("studio-preset-form","presets",key=>{if(key==="name"){library("presets","studio-preset-list",renderPresets);renderSelectors();}});
  bindEditor("studio-entry-form","entry",()=>{
    const book=get("worldbooks"),list=$("studio-entry-list");[...list.children].forEach((row,index)=>{const item=book.entries[index];row.querySelector("strong").textContent=item.name;row.querySelector("small").textContent="优先级 "+item.priority;row.querySelector("span").textContent=item.enabled?(item.constant?"◆":"◇"):"○";});
  });
  function newCharacter(){
    const card=clone(draft.characters[0]);Object.keys(card).forEach(key=>{if(typeof card[key]==="string")card[key]="";});
    Object.assign(card,{id:uid("char"),name:"新角色",tags:[],alternate_greetings:[],extensions:{},worldbook_ids:[],greeting_index:0,character_version:"1.0"});return card;
  }
  function newEntry(){return {id:uid("entry"),name:"新条目",keys:[],secondary_keys:[],selective:false,constant:false,enabled:true,case_sensitive:false,whole_words:false,priority:100,position:"before_character",depth:4,content:"",exclude_recursion:false};}
  function add(collection,item,render){draft[collection].push(item);selected[collection]=item.id;mark();renderSelectors();render();}
  function remove(collection,render){
    const item=get(collection);if(!item)throw new Error("请先选择一个项目");
    if(collection!=="worldbooks"&&draft[collection].length===1)throw new Error("至少保留一个项目");
    const field={characters:"character_id",personas:"user_persona_id",presets:"preset_id"}[collection];
    if(field&&Object.values(state.config.conversations).some(c=>c[field]===item.id))throw new Error("这个项目已绑定真实会话，请先在会话配置中解绑");
    if(collection==="worldbooks"){
      if(Object.values(state.config.conversations).some(c=>c.worldbook_ids?.includes(item.id)))throw new Error("此世界书已绑定真实会话，请先解绑");
      draft.global_worldbook_ids=draft.global_worldbook_ids.filter(id=>id!==item.id);
      draft.characters.forEach(card=>card.worldbook_ids=card.worldbook_ids.filter(id=>id!==item.id));
    }
    draft[collection]=draft[collection].filter(x=>x.id!==item.id);
    const active={characters:"active_character",personas:"active_persona",presets:"active_preset"}[collection];
    if(active&&draft[active]===item.id)draft[active]=draft[collection][0].id;
    selected[collection]="";mark();renderSelectors();render();toast("已从草稿移出；保存前可用“撤销未保存修改”恢复");
  }
  document.querySelectorAll("[data-studio-tab]").forEach(el=>el.onclick=()=>show(el.dataset.studioTab));
  for(const [suffix,field] of [["character","active_character"],["persona","active_persona"],["preset","active_preset"]])$("studio-default-"+suffix).onchange=()=>{draft[field]=$("studio-default-"+suffix).value;mark();};
  listen("studio-save",async()=>{if(!draft)throw new Error("角色库尚未加载");await save({roleplay:clone(draft)});});
  listen("studio-revert",()=>{draft=clone(state.config.roleplay);saved=clone(draft);clean();renderAll();toast("已恢复最近保存的角色库");});
  $("studio-search").oninput=()=>library("characters","studio-character-list",renderCharacters,$("studio-search").value);
  listen("studio-new-character",()=>add("characters",newCharacter(),renderCharacters));
  listen("studio-duplicate-character",()=>{const card=clone(get("characters"));card.id=uid("char");card.name+=" · 副本";add("characters",card,renderCharacters);});
  listen("studio-delete-character",()=>remove("characters",renderCharacters));
  listen("studio-new-persona",()=>add("personas",{id:uid("persona"),name:"新用户人设",description:""},renderPersonas));
  listen("studio-delete-persona",()=>remove("personas",renderPersonas));
  listen("studio-new-book",()=>{selected.entry="";add("worldbooks",{id:uid("book"),name:"新世界书",entries:[]},renderBooks);});
  listen("studio-delete-book",()=>{remove("worldbooks",renderBooks);renderCharacters();});
  $("studio-book-name").oninput=()=>{const book=get("worldbooks");if(book){book.name=$("studio-book-name").value;mark();library("worldbooks","studio-book-list",renderBooks);}};
  $("studio-book-global").onchange=()=>{const book=get("worldbooks");if(!book)return;draft.global_worldbook_ids=draft.global_worldbook_ids.filter(id=>id!==book.id);if($("studio-book-global").checked)draft.global_worldbook_ids.push(book.id);mark();};
  listen("studio-new-entry",()=>{const book=get("worldbooks");if(!book)throw new Error("请先创建世界书");const item=newEntry();book.entries.push(item);selected.entry=item.id;mark();renderBooks();});
  listen("studio-duplicate-entry",()=>{const book=get("worldbooks"),item=clone(book.entries.find(e=>e.id===selected.entry));item.id=uid("entry");item.name+=" · 副本";book.entries.push(item);selected.entry=item.id;mark();renderBooks();});
  listen("studio-delete-entry",()=>{const book=get("worldbooks");book.entries=book.entries.filter(e=>e.id!==selected.entry);selected.entry="";mark();renderBooks();});
  listen("studio-new-preset",()=>{const p=clone(draft.presets[0]);p.id=uid("preset");p.name="新生成预设";add("presets",p,renderPresets);});
  listen("studio-duplicate-preset",()=>{const p=clone(get("presets"));p.id=uid("preset");p.name+=" · 副本";add("presets",p,renderPresets);});
  listen("studio-delete-preset",()=>remove("presets",renderPresets));

  function downloadBlob(payload,name){
    const url=URL.createObjectURL(new Blob([JSON.stringify(payload,null,2)],{type:"application/json"}));
    const link=dom("a");link.href=url;link.download=name;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  function exportItem(kind,id,format="json"){
    if(dirty)throw new Error("请先保存当前草稿再导出单项；“导出工坊”可直接保存整个草稿");
    const link=dom("a");link.href="/api/roleplay/export?kind="+kind+"&id="+encodeURIComponent(id)+"&format="+format;link.download=id+"."+format;document.body.append(link);link.click();link.remove();
  }
  listen("studio-export-card",()=>exportItem("card",selected.characters));listen("studio-export-png",()=>exportItem("card",selected.characters,"png"));
  listen("studio-export-book",()=>exportItem("book",selected.worldbooks));listen("studio-export-preset",()=>exportItem("preset",selected.presets));
  listen("studio-export-library",()=>downloadBlob({format:"qq-companion-studio",version:1,data:draft},"character-studio.json"));
  listen("studio-import",()=>$("studio-file").click());
  function asDataURL(file){return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(new Error("文件读取失败"));reader.readAsDataURL(file);});}
  $("studio-file").onchange=action(async()=>{
    const file=$("studio-file").files[0];if(!file)return;
    try{
      const kind=$("studio-import-kind").value,maximum=kind==="card"?3:11;
      if(file.size>maximum*1024*1024)throw new Error("此类导入文件最大 "+maximum+" MB");
      const encoded=await asDataURL(file);
      const result=await api("roleplay/import",{kind,content:encoded.split(",")[1]});
      if(result.character){draft.characters.push(result.character);draft.worldbooks.push(...result.worldbooks);selected.characters=result.character.id;show("characters");}
      if(result.worldbook){draft.worldbooks.push(result.worldbook);selected.worldbooks=result.worldbook.id;selected.entry="";show("books");}
      if(result.preset){draft.presets.push(result.preset);selected.presets=result.preset.id;show("presets");}
      if(result.library){draft=result.library;selected={characters:"",personas:"",worldbooks:"",presets:"",entry:""};show("characters");}
      $("studio-import-notes").replaceChildren(...(result.warnings||[]).map(text=>dom("p",text)));
      $("studio-import-notes").hidden=!(result.warnings||[]).length;mark();renderAll();toast("已导入草稿；检查后点击“保存工坊设置”生效");
    }finally{$("studio-file").value="";}
  });
  listen("studio-upload-avatar",()=>$("studio-avatar-file").click());
  $("studio-avatar-file").onchange=action(async()=>{
    const file=$("studio-avatar-file").files[0];if(!file)return;
    try{
      if(file.size>3*1024*1024)throw new Error("头像最大 3 MB");
      const bytes=new Uint8Array(await file.slice(0,8).arrayBuffer());
      if([...bytes].join(",")!=="137,80,78,71,13,10,26,10")throw new Error("请选择 PNG 头像");
      get("characters").avatar="data:image/png;base64,"+(await asDataURL(file)).split(",")[1];mark();characterMeta();library("characters","studio-character-list",renderCharacters,$("studio-search").value);
    }finally{$("studio-avatar-file").value="";}
  });

  function selection(){return {character_id:$("studio-test-character").value,user_persona_id:$("studio-test-persona").value,preset_id:$("studio-test-preset").value};}
  function renderTest(){
    const box=$("studio-test-messages");box.replaceChildren();
    const card=get("characters",$("studio-test-character").value),user=get("personas",$("studio-test-persona").value);
    testHistory.forEach(message=>{
      for(const part of message.parts||[message.content]){
      const article=dom("article",undefined,"studio-chat-message "+message.role);
      article.append(dom("small",message.role==="assistant"?(card?.name||"角色"):(user?.name||"我")),dom("p",part));box.append(article);
      }
    });
    if(!testHistory.length)box.append(dom("p","用一句话开始这场对话。","studio-hint"));box.scrollTop=box.scrollHeight;
  }
  function resetTest(){
    testEpoch++;testHistory=[];testEmotion=null;
    const card=get("characters",$("studio-test-character").value),user=get("personas",$("studio-test-persona").value);
    if(card){const text=[card.first_mes,...card.alternate_greetings][card.greeting_index]||"";if(text)testHistory.push({role:"assistant",content:text.replaceAll("{{char}}",card.name).replaceAll("{{user}}",user?.name||"我")});}
    renderTest();$("studio-prompt-summary").textContent="试聊已准备好。预览不调用模型；发送试聊会调用 DeepSeek。";$("studio-prompt-blocks").replaceChildren();$("studio-lore-result").replaceChildren();$("studio-prompt-json").textContent="";
  }
  function requestBody(text="",live=false){return {
    ...(live?{}:{draft:clone(draft)}),mode:live?"live":"sandbox",chat:$("studio-test-chat").value,
    emotion_state:live?null:testEmotion,selection:live?{}:selection(),history:clone(testHistory.slice(-80)),text,
    use_memory:$("studio-test-memory").checked,proactive:$("studio-test-proactive").checked,
  };}
  function renderPrompt(compiled){
    $("studio-prompt-summary").textContent=[compiled.resolved.character,compiled.resolved.persona,compiled.resolved.preset].join(" / ")+"\n"+compiled.characters.toLocaleString()+" 字符 · "+compiled.messages.length+" 条模型消息 · 裁剪 "+compiled.dropped_messages+" 条\n会话可用工具："+((compiled.tool_names||[]).join("、")||"无")+"（独立试聊不执行工具）\n"+(compiled.warnings||[]).join("\n");
    if(compiled.context_layers?.enabled){
      const stats=Object.values(compiled.profile_retrieval||{});
      const source=stats.reduce((n,s)=>n+s.original_chars,0),core=stats.reduce((n,s)=>n+(s.core_chars||0),0),detail=stats.reduce((n,s)=>n+(s.detail_chars||0),0);
      $("studio-prompt-summary").textContent+="\n分层上下文：原卡 "+source+" 字符完整保存；常驻核心 "+core+" 字符；本轮检索原文 "+detail+" 字符。使用本地段落/关键词检索。";
    }
    const blocks=$("studio-prompt-blocks");blocks.replaceChildren();
    compiled.blocks.forEach(block=>{const row=dom("div",undefined,"studio-block"+(block.enabled?"":" is-disabled"));row.append(dom("span",names[block.id]),dom("small",block.enabled?block.chars.toLocaleString()+" 字符":"未启用"));blocks.append(row);});
    const lore=$("studio-lore-result");lore.replaceChildren(dom("h4","世界书 · "+compiled.lore.included.length+" 项已注入 / "+compiled.lore.used_chars+" 字符"));
    compiled.lore.included.forEach(item=>lore.append(dom("p",item.book+" / "+item.name+" · "+(item.wave?"递归第 "+item.wave+" 轮":"直接触发")+" · 优先级 "+item.priority)));
    compiled.lore.skipped.forEach(item=>lore.append(dom("p",item.name+" · "+item.reason,"studio-warning")));
    if(!compiled.lore.included.length)lore.append(dom("p","没有命中已绑定世界书中的条目。","studio-hint"));
    $("studio-prompt-json").textContent=JSON.stringify(compiled.messages,null,2);
  }
  listen("studio-preview",async()=>renderPrompt(await api("roleplay/preview",requestBody($("studio-test-text").value))));
  listen("studio-preview-live",async()=>{if(!$("studio-test-chat").value)throw new Error("请先选择一个参照会话");renderPrompt(await api("roleplay/preview",requestBody("",true)));});
  async function runTest(regenerate=false){
    if(busy)throw new Error("上一条试聊仍在生成，请稍候");
    const text=$("studio-test-text").value.trim();
    if(!regenerate&&!text&&!$("studio-test-proactive").checked)throw new Error("请先输入试聊内容，或勾选模拟主动搭话");
    const previous=clone(testHistory),epoch=testEpoch;
    if(regenerate){if(!testHistory.some(m=>m.role==="user"))throw new Error("先发送一条消息，再重新生成");if(testHistory.at(-1)?.role==="assistant")testHistory.pop();}
    const body=requestBody(regenerate?"":text);
    if(!regenerate&&text)testHistory.push({role:"user",content:text});renderTest();busy=true;
    $("studio-send-test").disabled=$("studio-regenerate").disabled=true;$("studio-send-test").textContent="正在生成…";
    try{
      const response=await api("roleplay/test",body);
      if(testEpoch!==epoch)return;
      if(response.result.emotion_state)testEmotion=response.result.emotion_state;
      if(response.result.reply)testHistory.push({role:"assistant",content:response.result.text,parts:response.result.messages});
      else toast("角色选择暂时保持安静，可在检查器查看当前上下文");
      $("studio-test-text").value="";renderTest();renderPrompt(response.prompt);
      if(response.result.emotion_state)$("studio-prompt-summary").textContent+="\n沙盒独立情绪："+response.result.emotion_state.labels.join("、")+" · 心动值 "+(response.result.emotion_state.flutter??0).toFixed(1)+" / 100。"+(response.result.timing_reason||"");
      if(response.result.reply_delays_seconds?.length)$("studio-prompt-summary").textContent+="\n模型回复节奏：首条目标 "+response.result.reply_delays_seconds[0]+" 秒；后续气泡间隔 "+(response.result.reply_delays_seconds.slice(1).join(" / ")||"无")+" 秒。沙盒直接展示结果；QQ实际发送执行此计划。";
    }catch(error){if(testEpoch===epoch){testHistory=previous;renderTest();}throw error;}
    finally{busy=false;$("studio-send-test").disabled=$("studio-regenerate").disabled=false;$("studio-send-test").textContent="发送试聊 · 调用 DeepSeek";}
  }
  $("studio-test-form").onsubmit=action(()=>runTest());listen("studio-regenerate",()=>runTest(true));listen("studio-reset-test",resetTest);
  for(const id of ["studio-test-character","studio-test-persona","studio-test-preset"])$(id).onchange=resetTest;
  $("studio-test-chat").onchange=()=>{$("studio-test-memory").checked=false;$("studio-test-memory").disabled=!$("studio-test-chat").value;$("studio-preview-live").disabled=!$("studio-test-chat").value;resetTest();};
  listen("studio-try-character",()=>{show("sandbox");$("studio-test-character").value=selected.characters;resetTest();});
  window.addEventListener("beforeunload",event=>{if(dirty){event.preventDefault();event.returnValue="";}});
  if(state?.config?.roleplay)sync(state.config.roleplay);
  return {sync,show};
})();
