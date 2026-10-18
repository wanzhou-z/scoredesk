'use strict';
// 只处理显示状态与动画，不在浏览器中重算比赛排名。
const qs = new URLSearchParams(location.search);
const app = document.getElementById('app');
const viewport = document.getElementById('viewport');
const model = { config: {}, options: {}, previous: new Map(), last: null, clockBase: 0, receivedAt: 0, clockFrozen: null, scrollAt: 0, scrollPhase: 'hold-top', scrollStart: 0, renderCount: 0, activeAnimations: 0, pausedUntil: 0, lastHint: '', hintThrottle: 0, marks: [], animBoost: false, refreshAsked: false,
  _pi: new Map(), followQueue: [], followTimes: [], followPlan: null, followErrors: [], pendingHold: false,
  // M23 时间轴（?timebar=1）：on=显示控件；hold=已脱离实时（拖过/播放中）；at=当前回放秒数；
  //   playing=播放中；token=丢弃过期的 seek 响应；dragging=正在拖滑块（不反写滑块）。
  timebar: { on: false, hold: false, at: null, playing: false, token: 0, dragging: false, timer: 0 } };
const esc = x => String(x ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function seconds(value) { const n = Math.max(0, Math.floor(value || 0)); return [Math.floor(n / 3600), Math.floor(n / 60) % 60, n % 60].map(x => String(x).padStart(2, '0')).join(':'); }
// ---------------------------------------------------------------------------
// 校徽加载重试（主控实测：预览服务刚重启时首帧并发请求可能失败，旧写法 onerror 直接
// visibility:hidden -> 永久隐藏，只能刷新手动救）。现在失败后按 400/1200/2500ms 重试最多 3 次
// （带 cache-buster，避开失败缓存），仍失败才淡出；**元素保留**（同样尺寸 -> 行高不跳、列线不动）。
// ---------------------------------------------------------------------------
const LOGO_RETRY_DELAYS = window.LOGO_RETRY_DELAYS || [400, 1200, 2500];
function logoRetry(img) {
  if (!img) return;
  const n = Number(img.dataset.retry || 0);
  const base = img.dataset.src || (img.getAttribute('src') || '').split('?')[0];
  if (!base) return;
  img.dataset.src = base;
  if (n < LOGO_RETRY_DELAYS.length) {
    img.dataset.retry = String(n + 1);
    img.dataset.state = 'retry';
    setTimeout(function () {
      img.src = base + '?retry=' + (n + 1) + '&t=' + Date.now();
    }, LOGO_RETRY_DELAYS[n]);
    return;
  }
  img.dataset.state = 'failed';        // 兜底：淡出但保留占位（不 visibility:hidden，避免布局跳动）
  img.style.opacity = '0';
}
function logoOk(img) { if (img) img.dataset.state = 'ok'; }

function option(key, fallback) { return qs.has(key) ? qs.get(key) : (model.config.view || {})[key] ?? fallback; }
function number(key, fallback, min, max) { const n = Number(option(key, fallback)); return Number.isFinite(n) ? Math.min(max, Math.max(min, n)) : fallback; }
function configure(c) {
  model.config = c;
  const view = c.view || {};
  const colors = Array.isArray(view.problem_colors) ? view.problem_colors : [];
  const medals = Array.isArray(view.medal_colors) ? view.medal_colors : [];
  // 优先用服务端给的 awards_str（team_ids 是字符串 -> 不丢精度）；没有就退回原来的 view.awards
  const awards = Array.isArray(view.awards_str) ? view.awards_str : (Array.isArray(view.awards) ? view.awards : []);
  model.options = { top: Math.floor(number('top', 0, 0, 10000)), rows: Math.floor(number('rows', 13, 0, 300)), scale: number('scale', 1, .4, 2), anim: String(option('anim', 1)) !== '0', scroll: String(option('scroll', 1)) === '1', scrollSpeed: number('scroll_speed', 24, 1, 200), scrollPause: number('scroll_pause', 2, .2, 30),
    // M11：排名上升跟随。?follow=1|0（默认 1）、?follow_pause=<秒>（默认 2.5）、?follow_max=<次/分钟>（默认 6）
    follow: String(option('follow', 0)) !== '0', followPause: number('follow_pause', 2.5, .2, 60), followMax: Math.floor(number('follow_max', 6, 0, 600)), problemColors: colors, medalColors: medals, awards };
  model.options.freeze = qs.get('freeze') || c.freeze_mode || 'operator';
  document.body.classList.toggle('overlay', String(option('overlay', 0)) === '1');
  document.body.classList.toggle('no-anim', !model.options.anim);
  // M11 取证开关：?classic=1 关掉本次新增的题格样式（用于 overlay 逐像素 A/B）。
  // M16：classic=1 同时**不挂**新皮肤类（body.m16）与主题类，于是 classic = 改动前旧视觉；
  //      `?theme=light|dark` 只决定挂哪个主题类，默认 light（仿照参照榜）。
  const classic = String(option('classic', 0)) === '1';
  const theme = String(option('theme', 'light')).toLowerCase() === 'dark' ? 'dark' : 'light';
  document.body.classList.toggle('classic', classic);
  document.body.classList.toggle('m16', !classic);
  document.body.classList.toggle('theme-light', !classic && theme === 'light');
  document.body.classList.toggle('theme-dark', !classic && theme === 'dark');
  // M17：?cells=ref（默认，参照榜那种"填充 + 次数 + 时间"题格）；?cells=ours 回到我们的"数字上色"。
  //       ?star=problem 让一血星标跟题目色（默认照参照榜：金 ★ #ffc107）。
  const cells = String(option('cells', 'ref')).toLowerCase();
  document.body.classList.toggle('cells-ref', !classic && cells !== 'ours');
  document.body.classList.toggle('cells-ours', !classic && cells === 'ours');
  document.body.classList.toggle('star-problem', !classic && String(option('star', '')).toLowerCase() === 'problem');
  // M17：琥珀格字色。默认深色字（#231a02，≈9:1）；?postink=white 照抄参照榜 CSS 原值（白字，1.79:1）。
  document.body.classList.toggle('postink-white', !classic && String(option('postink', '')).toLowerCase() === 'white');
  // M23：?timebar=1 才显示「滚榜时间轴」（时间码 + 可拖滑块 + 播放键）。不传 = 与以前完全一样。
  model.timebar.on = !classic && String(option('timebar', 1)) === '1';
  document.body.classList.toggle('timebar-on', model.timebar.on);
  // M17（模板 = 滚榜页 /roll）：题格配色默认用**滚榜页线上实测值**；?palette=rank 切回 rank 页 CSS 原值。
  const pal = String(option('palette', '')).toLowerCase();
  document.body.classList.toggle('palette-roll', !classic && pal === 'roll');
  model.options.cells = classic ? 'ours' : (cells === 'ours' ? 'ours' : 'ref');
  let font = String(option('font', 'yahei')).trim();
  if (font.toLowerCase() !== 'yahei') document.body.style.fontFamily = '"' + font.replace(/["\\\r\n]/g, '') + '", PTAYaHei, "Microsoft YaHei", sans-serif';
  for (const [k, v] of Object.entries(c.theme || {})) document.documentElement.style.setProperty('--' + k.replaceAll('_', '-'), v);
}
function clock(s) {
  const p = s.presentation || {}, c = p.contest || {}, elapsed = p.elapsed ?? Math.max(0, Date.now()/1000 - (c.start_epoch || Date.now()/1000));
  model.clockBase = elapsed; model.receivedAt = performance.now();
  // M26 标题取值链（用户口径）：?title= 参数 → 配置里的 title（GUI 填的）→ PTA 比赛名 → 默认「实时榜单」。
  const cfgTitle = String(model.config.title || '').trim();
  const title = qs.get('title') || cfgTitle || c.title || 'ScoreDeck · 实时榜单';
  document.getElementById('title').textContent = title; document.title = title;
  // M25：副标题固定，不再因为「回放演练 / 拖时间轴」变成「回放演练 ×N」（用户 2026-10-17：不要这个。时间轴本身已经标出回放时刻）。
document.getElementById('subtitle').textContent = '实时排名（数据源同步）';
  // M23 表格上方的标题栏（照参照榜左上角）：与页面大标题同一条取值链。
  const barName = qs.get('title') || cfgTitle || c.title || 'ScoreDeck · 实时榜单';
  const barEl = document.getElementById('contestTitle');
  if (barEl && barEl.textContent !== barName) barEl.textContent = barName;
  model.duration = Number(c.duration_seconds || 10800) || 10800;
  updateClock();
}
function updateClock() {
  if (!model.last) return;
  const s = model.last, p = s.presentation || {}, c = p.contest || {}, duration = Number(c.duration_seconds || 10800);
  const now0 = model.clockFrozen ?? performance.now();
  // M23：时间轴拖到某一刻 / 暂停播放时，时钟不再往前跑（画面停在那一帧）。
  const paused = p.held || (model.timebar.hold && !model.timebar.playing);
  const advance = paused ? 0 : (now0 - model.receivedAt)/1000 * Number(p.speed || 1);
  const now = Math.min(duration, Math.max(0, model.clockBase + advance));
  document.getElementById('elapsed').textContent = seconds(now);
  // M17：参照榜工具条里的"比赛时间进度条"（填充宽度 + 圆钮位置）。纯视觉，不影响任何滚动/数据。
  const fill = document.getElementById('progressFill'), knob = document.getElementById('progressKnob');
  if (fill || knob) {
    const pct = duration > 0 ? Math.min(100, Math.max(0, (now / duration) * 100)) : 0;
    if (fill) fill.style.width = pct.toFixed(2) + '%';
    if (knob) knob.style.left = pct.toFixed(2) + '%';
  }
  // M23：时间轴的「时间码 + 滑块位置」跟着当前回放时刻走（拖动中不反写滑块，避免手感打架）。
  updateTimeBar(now, duration);
  const label = document.getElementById('freezeLabel'), text = document.getElementById('freezeText');
  // M11 文案（要短，"封榜中 · 已永久冻结" 在窄状态栏里会换行跑出框）：
  // 观众视角永远是「封榜中 · 未解榜」；?thaw=1 只是保留的调试能力，界面上不再出现「裁判视角」字样。
  if (s.frozen && s.presentation?.thaw) { label.textContent = '封榜中 · 结果已揭示'; text.textContent = ''; }
  else if (s.frozen && s.presentation?.contest_over) { label.textContent = '未解榜'; text.textContent = ''; }
  else if (s.frozen) { label.textContent = '封榜中 · 未解榜'; text.textContent = ''; }
  else if (s.presentation?.contest_over) { label.textContent = '全部成绩已揭示'; text.textContent = '比赛结束'; }
  else if (p.freeze_at != null) { label.textContent = '封榜倒计时'; text.textContent = seconds(Math.max(0, p.freeze_at - now)); }
  else { label.textContent = '榜单状态'; text.textContent = '实时公开'; }
  // M16：把「封榜中 / 已结束」落成 body 类，纯视觉用 —— 状态栏那块换成实色强调，
  // 让投屏远看也能一眼看到封榜状态（不改任何数据/协议语义；classic 下无对应 CSS，零像素影响）。
  document.body.classList.toggle('is-frozen', !!s.frozen);
  document.body.classList.toggle('is-finished', !!s.presentation?.contest_over);
}
function visibleCell(c, t, s, locked) {
  const v = {...c};
  // 观众题格已经由服务端用封榜时刻的数据计算，不能再遮成问号。
  if (s.presentation?.freeze_mode === 'audience' && s.frozen) return v;
  // M1 历史快照可能仍含 locked；解封只恢复显示，不改变服务端积分。
  if (!s.frozen && v.state === 'locked') v.state = 'ac';
  if (s.frozen) {
    const after = c.time != null && Number(c.time) >= Number(s.freeze_at);
    const preAC = c.time != null && Number(c.time) < Number(s.freeze_at) && c.state === 'ac';
    if (!preAC && (after || locked.has(t.team_id + ':' + c.problem_id))) { v.state = 'locked'; v.time = null; v.wrong = 0; }
  }
  return v;
}
// ---------------------------------------------------------------------------
// M11 题格渲染
//   ① 判题中（状态 pending）     -> 绿/黑交替闪烁（cell-blink，0.5s 周期）
//   ② 出结果 AC                  -> 稳定绿色（沿用 + / +N / 一血星标）
//   ③ 出结果非 AC                -> 稳定红色（沿用 −N）
//   ④ 封榜后的提交               -> 黄色段（`.seg-post`）。封榜前的 WA 仍红，于是同一格
//      会出现「红 1 + 黄 1」两段并排（`.cell-split`；刀口必须清晰，不许糊在一起）。
//   服务端新增字段（只增不改）：wrong_before / wrong_after / post / pending / seen。
// ---------------------------------------------------------------------------
function preWrong(c) {                       // 封榜前的非 AC 次数（旧数据没有新字段时退回 wrong）
  const v = Number(c.wrong_before);
  return Number.isFinite(v) ? v : Math.max(0, Number(c.wrong || 0));
}
function postWrong(c) { return Math.max(0, Number(c.wrong_after || 0)); }
function isPost(c, s) {                      // 这一格有"封榜后提交"（观众视角下算黄格）
  return !!c.post && !s.presentation?.thaw;
}
function judgeSeg(c, s) {                    // 封榜快照里"出结果"的那一段
  const w = preWrong(c);
  if (c.state === 'ac') return { state: 'ac', wrong: w, time: c.time, label: w > 0 ? '+' + w : '+' };
  if (c.state === 'wrong') return { state: 'wrong', wrong: w, time: null, label: '−' + w };
  return null;
}
function postSeg(c) {                        // 封榜后的提交：黄，永远不揭晓结果
  const parts = [];
  if (c.pending) parts.push('判题中');
  parts.push(postWrong(c) > 0 ? '−' + postWrong(c) : '封榜后提交');
  const wrongs = postWrong(c);
  const label = wrongs > 0 ? '+' + wrongs : '+';
  // M17 追加：后端新增键 post_last_minutes（封榜后最后一次提交的相对分钟数，整数）；
  // 有它就按参照榜那样显示 `[次数][299']`，没有就只显示次数（不会出现 undefined / 空块）。
  const mins = Number.isFinite(Number(c.post_last_minutes)) && c.post_last_minutes != null ? Math.floor(Number(c.post_last_minutes)) : null;
  return { state: 'post', wrong: wrongs, time: mins != null ? mins * 60 : null, minutes: mins, label,
           title: '封榜后提交 ' + (postWrong(c) + Number(c.pending ? 1 : 0)) + ' 次（' + parts.join(' · ') + '）· 结果不揭晓'
                  + (mins != null ? ' · 最后提交 ' + mins + "'" : '') };
}
function segHtml(seg, first) {
  const cls = ['cell-seg', seg.state, seg.state === 'ac' && seg.wrong > 0 ? 'has-wrong' : '',
               seg.state === 'ac' && first ? 'first-blood' : ''].filter(Boolean).join(' ');
  const star = seg.state === 'ac' && first ? '<span class="star" title="本题一血">★</span>' : '';
  if (model.options.cells === 'ref') return '<span class="' + cls + '" data-seg="' + seg.state + '" title="' + esc(seg.title || '') + '">' + refSegInner(seg) + star + '</span>';
  const time = seg.state === 'ac' && seg.time != null ? '<span class="ac-time" aria-hidden="true">' + Math.floor(seg.time / 60) + '</span>' : '';
  return '<span class="' + cls + '" data-seg="' + seg.state + '" title="' + esc(seg.title || '') + '">' +
         '<span class="cell-label">' + seg.label + '</span>' + time + star + '</span>';
}
// ---------------------------------------------------------------------------
// M17 追加：?cells=ref（默认）= 参照榜那种"填充 + 次数 + 时间"的格子。
//   .pro-submit-cnt  提交次数（参照榜是一个底色略深的琥珀小方块）
//   .time-brief      默认显示的时间（分钟 + '），.time-full 里放完整描述（hover 用，等宽）
//   ?cells=ours      回到我们原来的"数字上色"（+N / −N / · / ?）
//   ?star=problem    一血星标跟题目色（默认照参照榜：金 ★ #ffc107）
// ---------------------------------------------------------------------------
function submitCount(c, seg) {               // 这一段的"提交次数"
  const w = Math.max(0, Number(seg.wrong || 0));
  if (seg.state === 'post') return Math.max(1, w + Number(c.pending ? 1 : 0));
  if (seg.state === 'ac') return w + 1;
  return Math.max(1, w);
}
function totalSubmits(c) {                       // 该题**总提交次数**（照参照榜 pro-submit-cnt 口径）
  const before = (c.wrong_before === null || c.wrong_before === undefined) ? null : Number(c.wrong_before);
  const after = (c.wrong_after === null || c.wrong_after === undefined) ? null : Number(c.wrong_after);
  // M44（用户 2026-10-18）：**通过之后的所有提交都不做数**（不计数、也不进罚时）。
  //   所以 AC 格只算「通过前错几次 + 通过那次」，wrong_after 一律丢掉。
  if (c.state === 'ac') {
    const beforeN = (before !== null) ? Math.max(0, before) : Math.max(0, Number(c.wrong || 0));
    return Math.max(1, beforeN + 1);
  }
  let n;
  if (before !== null || after !== null) n = Math.max(0, before || 0) + Math.max(0, after || 0);
  else n = Math.max(0, Number(c.wrong || 0));
  if (c.pending) n += 1;                          // 还在判题的那次也算
  // M48：编译错误不算次数 —— 只交过 CE 的格子就该显示 0，不再兜底成 1。
  return Math.max(0, n);
}
function lastMinutes(c) {                      // 后端新增键：这一格最后一次提交的分钟数（只增不改）
  const v = c && c.last_submit_minutes;
  return (v === null || v === undefined || !Number.isFinite(Number(v))) ? null : Math.floor(Number(v));
}
function refSegInner(seg, c) {
  // 用户口径：每格只有一段 -> 次数 = **该题总提交次数**，时间 = 该题**最后一次提交**的分钟数。
  //   封榜后：post_last_minutes；其它：last_submit_minutes（AC 优先用它自己的 time）。
  const cnt = '<span class="pro-submit-cnt">' + totalSubmits(c || {}) + '</span>';
  let mins = null;
  if (seg && seg.state === 'post') {
    const v = c ? c.post_last_minutes : null;
    mins = (v === null || v === undefined || !Number.isFinite(Number(v))) ? null : Math.floor(Number(v));
  }
  if (mins == null && seg && seg.time != null) mins = Math.floor(seg.time / 60);
  if (mins == null) mins = lastMinutes(c);
  const brief = mins != null ? '<span class="time-brief">' + mins + "'</span>" : '';
  const full = '<span class="time-full">' + esc((seg && (seg.title || seg.label)) || '') + '</span>';
  return cnt + brief + full;   // 单段：次数块 + 分钟'（真没有分钟数据时只有次数块，不出现 undefined/空块）
}
function cellInner(c, s, isFirst, isPostFrozen) {
  const ref = model.options.cells === 'ref';
  const post = isPost(c, s);
  if (isPostFrozen && c.pending && !post) return { cls: 'cell pending-cell' + (ref ? ' pro-judging' : ''), html: '<span class="cell-label">·</span>', post: false, pending: true };
  if (isPostFrozen) {
    // 用户更正：**封榜后一律整格琥珀，不再两段式**（红+黄作废）。
    //   ref（默认）每题格只有一段底色；?cells=ours 仍保留旧的分段语义。
    const judged = judgeSeg(c, s);
    if (ref) {
      const letter = c.label || c.problem_id || '';
      if (post) {
        // 封榜后有提交（不管 wrong_before 是 0 还是 >0）-> 整格琥珀 + [总次数][最后提交分钟']
        const seg = { state: 'post', wrong: preWrong(c) + postWrong(c), time: null, label: '+' };
        return { cls: 'cell has-post' + (c.pending ? ' pro-judging' : '') + (isFirst ? '' : ''),
                 html: refSegInner(seg, c), post: true, pending: false };
      }
      if (judged) {
        // 封榜前出的结果（AC / WA）-> 单段：绿 / 深红
        const seg = { state: judged.state, wrong: judged.wrong, time: judged.time, label: judged.label };
        const first = judged.state === 'ac' && isFirst;
        return { cls: 'cell ' + judged.state + (first ? ' first-blood' : ''),
                 html: refSegInner(seg, c) + (first ? '<span class="star" title="本题一血">★</span>' : ''),
                 post: false, pending: false };
      }
      if (c.pending) return { cls: 'cell pending-cell pro-judging', html: refSegInner({ state: 'pending', wrong: 0, time: null, label: '·' }, c), post: false, pending: true };
      // 封榜视角下"这题压根没提交" -> 灰格居中题目字母
      return { cls: 'cell pending', html: '<span class="cell-letter">' + esc(letter) + '</span>', post: false, pending: false };
    }
    const yellow = post ? postSeg(c) : null;
    if (!judged && !yellow) {
      return { cls: 'cell pending', html: '<span class="cell-label">·</span>', post: false, pending: false };
    }
    const segs = [];
    if (judged) segs.push(segHtml(judged, isFirst));
    if (yellow) segs.push(segHtml(yellow, false));
    const only = segs.length === 1 ? (judged ? judged.state : 'locked') : '';
    const cls = 'cell' + (only ? ' ' + only : '') + (segs.length > 1 ? ' cell-split' : '') +
                (judged && judged.state === 'ac' && judged.wrong ? ' has-wrong' : '') +
                (judged && judged.state === 'ac' && isFirst ? ' first-blood' : '') + (yellow ? ' has-post' : '');
    return { cls, html: segs.join(''), post: !!yellow, pending: false };
  }
  // 实时视角（含 ?thaw=1 / freeze=operator）
  if (c.state === 'pending' && c.pending) return { cls: 'cell pending-cell blink' + (ref ? ' pro-judging' : ''), html: '<span class="cell-label">·</span>', post: false, pending: true };
  if (c.state === 'pending') {
    // 未提交 = 灰格（#adb5bd）居中显示**该题字母**（用户逐条整改 3）；?cells=ours 仍是 `·`
    const letter = c.label || c.problem_id || '';
    return { cls: 'cell pending', html: ref ? '<span class="cell-letter">' + esc(letter) + '</span>' : '<span class="cell-label">·</span>', post: false, pending: false };
  }
  // WA：ref 模式下按"次数 + 时间"渲染（照样本 3 + 182'）
  
  const wrongs = Math.max(0, Number(c.wrong || 0));
  const label = c.state === 'ac' ? (wrongs > 0 ? '+' + wrongs : '+') : c.state === 'locked' ? '?' : c.state === 'wrong' ? '−' + wrongs : '·';
  const time = c.state === 'ac' && c.time != null ? '<span class="ac-time" aria-hidden="true">' + Math.floor(c.time / 60) + '</span>' : '';
  const star = isFirst && c.state === 'ac' ? '<span class="star" title="本题一血">★</span>' : '';
  const cls = 'cell ' + c.state + (c.state === 'ac' && wrongs ? ' has-wrong' : '') + (isFirst && c.state === 'ac' ? ' first-blood' : '');
  if (ref) {
    const seg = { state: c.state, wrong: wrongs, time: c.time, label,
                  minutes: c.state === 'ac' && c.time != null ? null : lastMinutes(c) };
    return { cls, html: refSegInner(seg, c) + star, post: false, pending: false };
  }
  return { cls, html: '<span class="cell-label">' + label + '</span>' + time + star, post: false, pending: false };
}
function cellTitle(c, s, inner) {
  if (inner.pending) return s.frozen ? '封榜后提交 · 判题中 · 结果不揭晓' : '判题中（等待评测结果）';
  return c.state === 'ac'
    ? (Math.max(0, Number(c.wrong || 0)) > 0 ? '通过 · 通过前被拒 ' + c.wrong + ' 次 · 第 ' + (Number(c.wrong) + 1) + ' 次提交通过'
                  : '通过 · 一次通过 · 第 1 次提交通过')
    : String(c.label ?? '');
}
function contrast(hex) {
  const m = String(hex || '').replace('#','');
  if (!/^[0-9a-f]{6}$/i.test(m)) return '#ffffff';
  const rgb = [0,2,4].map(i => parseInt(m.slice(i,i+2),16)/255).map(x => x <= .03928 ? x/12.92 : Math.pow((x+.055)/1.055,2.4));
  return (0.2126*rgb[0]+0.7152*rgb[1]+0.0722*rgb[2]) > .48 ? '#07131d' : '#ffffff';
}
// ---------------------------------------------------------------------------
// M11 排名上升 → 滚动跟随
//   触发：两帧数据之间某队 rank 变小（= 上升）。
//   选择：同时多队上升时，一次最多处理 2 个（1 个主跟随 + 1 个待命），按「上升幅度大 →
//         最近一次通过时间更晚」排序；同一队若又上升会顶掉待命位。
//   动作：自动滑动开 → 平滑滚到该行并停留 follow_pause 秒（默认 2.5s），然后继续乒乓；
//         自动滑动关 → 不滚动，只保留 ↑ + 行强调（人工接管时不许画面自己动）。
//         anim=0 → 完全静止（不滚也不闪烁）；follow=0 → 整条链路关闭（对照用）。
//   防刷屏：follow_max 次/分钟（默认 6，滑动窗口），超限只强调不滚动。
// ---------------------------------------------------------------------------
function followDetect(list) {
  const previous = model.previous, out = new Map();
  if (!previous.size) return out;
  for (const t of list) {
    const old = previous.get(t.team_id);
    if (!old || !(t.rank < old.rank)) continue;
    let latest = 0;
    for (const c of t.problems) if (c.time != null && Number(c.time) > latest) latest = Number(c.time);
    out.set(t.team_id, { delta: old.rank - t.rank, from: old.rank, to: t.rank, latest, team_name: t.team_name });
  }
  return out;
}
function detectFollow(list, deltas, s) {
  const opt = model.options, now = performance.now();
  for (const [id, d] of deltas) {
    if (model.followQueue.some(q => q.id === id)) continue;
    model.followQueue.push({ id, ...d, at: now });
    if (model.followQueue.length > 2) {
      model.followQueue.sort((a, b) => (b.delta - a.delta) || (b.latest - a.latest));
      model.followQueue.length = 2;                        // 一次最多 2 个：主 + 待命
    }
  }
  const started = decodeURIComponent((qs.get('follow_team') || '').trim());
  if (!model.followQueue.length) return;
  let item;
  if (started) {
    // 取证/演示用：只跟随指定的这一队（?follow_team=<team_id 或队名>），其余上升照旧只强调。
    item = model.followQueue.find(q => q.id === started || q.team_name === started);
    if (!item) return;
    model.followQueue = model.followQueue.filter(q => q !== item);
  } else {
    item = model.followQueue.shift();
  }
  model.followTimes = model.followTimes.filter(t => now - t < 60000);
  const limiter = opt.followMax <= 0 ? true : model.followTimes.length < opt.followMax;
  // M31（用户 2026-10-17 直播中）：名次上升的跟随滑动**独立于 anim / 乒乓滚动开关**。
  //   以前这里要求 animOK() && opt.scroll，而发行版配置是 anim=0、scroll=0（画面静止），
  //   于是「有人 AC、名次上升」时画面一动不动。现在只要 follow=1、没被冻结、没超频、能滚就滑。
  const willScroll = !!(opt.follow && autoOn() && !autoFrozen() && limiter && scrollable());
  if (opt.follow && animOK() && opt.scroll && autoOn() && !limiter) model.followErrors.push({ at: +now.toFixed(1), kind: 'rate-limited', team: item.id, delta: item.delta });
  if (!opt.follow || !animOK()) model.followErrors.push({ at: +now.toFixed(1), kind: !opt.follow ? 'follow-off' : 'anim-off', team: item.id, delta: item.delta });
  if (willScroll) model.followTimes.push(now);
  const idx = list.findIndex(t => t.team_id === item.id);
  const kids = document.getElementById('rows').children;
  const gap = parseFloat(getComputedStyle(document.getElementById('rows')).rowGap) || 4;
  const rowH = (kids.length && kids[0].getBoundingClientRect().height) || 51;
  const stride = rowH + gap, max = maxScroll();
  // 行尽量居中；上面留一行，靠近两端时各自贴边。
  const centered = Math.max(0, (idx - 1) * stride);
  const target = Math.max(0, Math.min(max, centered));
  const rowTop = idx * stride;
  model.followPlan = { team: item.id, teamName: item.team_name, delta: item.delta,
    from: item.from, to: item.to, index: idx, rowTop: +rowTop.toFixed(2), stride: +stride.toFixed(2),
    target: +target.toFixed(2), willScroll, limit: { used: model.followTimes.length, max: opt.followMax },
    at: +now.toFixed(1), scrollTopBefore: viewport.scrollTop, frozen: !!s.frozen };
  if (!willScroll) return;
  model.scrollPhase = 'follow';
  model.scrollFollowFrom = viewport.scrollTop;
  model.scrollFollowTarget = target;
  model.scrollFollowAt = now;
  model.pendingHold = false;
  return item;
}
function followHold(phase) {
  const now = performance.now();
  if (model.followPlan) model.followPlan.settled = +now.toFixed(1);
  model.scrollAt = now;
  model.pendingHold = true;                                // 停留 follow_pause 秒后继续乒乓
  mark('follow-' + phase);
}
function followTick(time) {
  const dur = 420, max = maxScroll();
  const p = Math.min(1, (time - model.scrollFollowAt) / dur);
  const eased = 1 - Math.pow(1 - p, 3);
  viewport.scrollTop = model.scrollFollowFrom + (model.scrollFollowTarget - model.scrollFollowFrom) * eased;
  if (p >= 1 || Math.abs(viewport.scrollTop - model.scrollFollowTarget) < .5)
    { viewport.scrollTop = model.scrollFollowTarget; followHold('settled');
      // M31：anim=0（不乒乓）时没人接手相位，这里收回到 hold-top，免得一直重进 follow。
      if (!animOK()) model.scrollPhase = 'hold-top'; }
}
function medalFor(rank) {
  // M17 口径纠正（用户）：**实时榜单按当前排名的"名次区间"着色** —— 用每个奖项的 count。
  //   实时榜是过程中随时在动的，队名/名次一直在变，所以不能按赛后滚榜的"勾选名单(team_ids)"上色；
  //   同一奖项同时有 count 和 team_ids 时：滚榜读名单、实时榜读人数（这里只读 count）。
  //   第 1 个奖 -> 金、第 2 个 -> 银、第 3 个 -> 铜、第 4 个往后不参与着色。
  //   count=0 的奖项覆盖不到任何名次（= 实时榜上不上色），这是"实时榜读人数"的直接结果。
  //   index 是 **1 基**（与 CSS 的 .medal-1/2/3 对齐）。
  const awards = Array.isArray(model.options.awards) ? model.options.awards : [];
  let start = 1;
  for (let i = 0; i < Math.min(3, awards.length); i++) {
    const count = Math.max(0, Math.floor(Number((awards[i] || {}).count || 0)));
    if (count > 0 && rank >= start && rank < start + count)
      return { index: i + 1, color: model.options.medalColors[i] || ['#D4AF37', '#A8B0B8', '#CD7F32'][i] || '#D4AF37' };
    start += count;
  }
  return null;
}
function rowCellHtml(t, c, old, s, isFirst, allowAnimation) {
  const frozenView = !!(s.frozen && s.presentation?.freeze_mode === 'audience');
  const inner = cellInner(c, s, isFirst, frozenView);
  const before = old && old.problems.find(x => x.problem_id === c.problem_id);
  const changedAC = c.state === 'ac' && (!before || before.state !== 'ac');
  const changedWrong = c.state === 'wrong' && before && (before.state !== 'wrong' || before.wrong !== c.wrong);
  const changedPending = c.pending && !(before && before.pending);
  const changedPost = inner.post && !(before && before.post);
  // M8：关掉自动滑动后行容器不再移动，因此只保留「颜色类」动效（变绿/变红/闪烁），
  // 不叠加 acSlide 的 translateY —— 否则会留下位移动画残留（画面自己动）。
  let cls = inner.cls;
  if (c.pending) cls += ' blink';                 // ④ 封榜后仍在判题：黄格也要绿黑闪烁
  if (allowAnimation && !frozenView) {
    // M30（用户 2026-10-17 直播中）：新 AC 的题格滑入一下，别啪地直接出现。
    //   只在有上一帧数据时才加（首次加载/刷新页面不表演，免得到处乱跳）；
    //   ?acanim=0 可整体关掉，默认开。
    if (changedAC && model.hasPrev && acAnimOn()) cls += ' ac-in';
    if (changedAC) cls += ' flash' + (autoOn() ? ' ac-slide' : '');
    if (changedWrong) cls += ' wrong-flash';
    if (changedPending) cls += ' pending-pulse';
  }
  if (inner.post) cls += (allowAnimation && changedPost) ? ' post-flash' : ' post-steady';
  const color = model.options.problemColors[model._pi.get(c.problem_id)] || '#60758f';
  const text = contrast(color);
  const title = cellTitle(c, s, inner);
  // M17（滚榜页模板）：题目格与表头的题目列都显式占第 (pi+4) 列 —— 前三列是 名次 / 标志 / 队伍。
  const pi = model._pi.get(c.problem_id) || 0;
  return '<div class="' + cls + '" data-state="' + (inner.pending ? 'pending' : c.state) + '" data-post="' + (inner.post ? '1' : '0') +
         '" style="--pi:' + pi + ';--problem-color:' + color + ';--problem-text:' + text + ';--reveal-delay:' + (model._pi.get(c.problem_id) * 110) + 'ms" title="' + esc(title) + (isFirst ? ' · 一血' : '') + '">' + inner.html + '</div>';
}
// M23：表头每题的「AC 队数/提交总数」。后端 problems[].ac_teams / subs 优先（口径见
// board_core.problem_stats：AC 不同队伍数 / 提交总次数，封榜后仍算）；旧 state 没有键时按榜单现算兜底。
function problemStat(p, idx, s) {
  if (p && p.ac_teams != null && p.subs != null) {
    const ac = Number(p.ac_teams), subs = Number(p.subs);
    if (Number.isFinite(ac) && Number.isFinite(subs)) return ac + '/' + subs;
  }
  let ac = 0, seen = 0;
  for (const t of (s.board || [])) {
    const c = t.problems && t.problems[idx];
    if (!c) continue;
    if (c.state === 'ac') ac++;
    if (c.seen || c.pending || c.post || c.state !== 'pending' || Number(c.wrong || 0) > 0 || c.time != null) seen++;
  }
  return ac + '/' + seen;
}
function render(s) {
  // 展示层按比赛时间封榜，API 后台排名仍保持 M1 原始结果。
  s = {...s, frozen: s.presentation?.frozen ?? s.frozen};
  const wasFrozen = !!(model.last && model.last.frozen);
  const thaw = wasFrozen && !s.frozen;
  model.last = s;
  clock(s);
  const timestamp = s.last_poll || s.updated;
  document.getElementById('updated').textContent = timestamp ? new Date(timestamp).toLocaleTimeString('zh-CN', {hour12:false}) : '--';
  document.getElementById('channel').textContent = s.channel || '--';
  const problems = s.problems || [];
  // M17（模板换成参照榜**滚榜页 /roll**）：表头只有 「排名/Rank」+「标志/Logo」+ 题目字母；
  //      题数/罚时/队伍 不再占表头列（滚榜页把它们放在数据行左侧的上下两栏里）。
  //      `?classic=1`（旧视觉）仍输出旧表头结构，保证与改动前逐像素可比。
  const classicView = document.body.classList.contains('classic');
  let headHtml;
  if (classicView) {
    headHtml = '<span>名次</span><span class="team-head">队伍 / 学号</span><span class="num-head">解题</span><span class="num-head">罚时</span>' +
      problems.map((p, i) => { const color = model.options.problemColors[i] || '#60758f'; return '<span class="problem-head" style="--problem-color:' + color + ';--problem-text:' + contrast(color) + '">' + esc(p.label) + '</span>'; }).join('');
  } else {
    // M23（学参照榜实时榜单页）：每题字母下面一行 11px muted 等宽小字「AC 队数/提交总数」；
    //   前几列中英双行小标题：排名/Team Rank、题数/Solved、罚时/Penalty、标志/Logo、队伍/Team。
    //   数据优先用后端新增键 problems[].ac_teams / subs；旧 state 没有这两个键时按榜单现算兜底。
    const stat = problems.map((p, i) => problemStat(p, i, s));
    headHtml =
      '<span class="rank-col head-rank"><b>排名</b><small>Team Rank</small></span>' +
      '<span class="rank-col head-solve"><b>题数</b><small>Solved</small></span>' +
      '<span class="rank-col head-penalty"><b>罚时</b><small>Penalty</small></span>' +
      '<span class="rank-col head-logo"><b>标志</b><small>Logo</small></span>' +
      '<span class="rank-col head-team"><b>队伍</b><small>Team</small></span>' +
      problems.map((p, i) => '<span class="rank-col problem-head" style="--pi:' + i + '"><b class="problem-header-title">' + esc(p.label) + '</b><small class="problem-header-stats">' + stat[i] + '</small></span>').join('');
  }
  document.getElementById('headRow').innerHTML = headHtml;
  model._pi = new Map(problems.map((p, i) => [String(p.id), i]));
  const opt = model.options;
  let list = (s.board || []).slice(0, opt.top || undefined);
  const candidates = list.length;
  model.shown = list.length;   // M28：真正上屏的队伍数（top/rows 截断之后；空行补位不算）
  // 固定行数既限制展示数量，也补空行；滚动时只改变内部可见区域。
  if (opt.rows && !opt.scroll) list = list.slice(0, opt.rows);
  const first = new Set(((s.presentation || {}).first_to_solve || []).map(p => p.team_id + ':' + p.problem_id));
  const locked = new Set(((s.presentation || {}).locked_cells || []));
  const previous = model.previous, next = new Map();
  // M11：先在两帧数据之间检测「排名上升」，再决定要不要滚到那一行。
  //   M23：时间轴在"脱离实时"（拖动/暂停/播放 seek）期间不做跟随滚动 —— 否则拖一下画面
  //   会被"名次上升"带着滚走；恢复实时后照常。
  const deltas = model.timebar.hold ? new Map() : followDetect(list);
  detectFollow(list, deltas, s);
  let animationSlots = model.options.anim ? 8 : 0;   // M30：名额 3 -> 8，同一帧多题 AC 也各自动
  if (model.timebar.hold) animationSlots = 0;   // M23：seek/播放时不叠加闪烁/滑入动效
  // M35：先记下每行现在的位置（FLIP 动画的第一帧），重排后再滑过去。
  const flipBefore = new Map();
  const flipMs = model.timebar.hold ? 0 : rankMoveMs();
  if (flipMs > 0) {
    document.querySelectorAll('#rows .board-row[data-team]').forEach(el => {
      flipBefore.set(el.dataset.team, el.getBoundingClientRect().top);
    });
  }
  document.getElementById('rows').innerHTML = list.map((t, i) => {
    const old = previous.get(t.team_id), d = deltas.get(t.team_id), up = !!d;
    // M33（用户 2026-10-18 直播）：同一队同一题**一旦 AC 就不允许回退**成未通过/未提交。
    //   拉取有时间差，旧数据偶尔会把已经变绿的格又画回红色（停留几秒才恢复）——这里直接钉住。
    //   时间轴拖动（seek）时不钉，按那一刻的真实状态显示。
    const cells = t.problems.map(c => visibleCell(c, t, s, locked)).map(c => {
      if (model.timebar.hold) return c;
      const b = old && old.problems && old.problems.find(x => x.problem_id === c.problem_id);
      if (b && b.state === 'ac' && c.state !== 'ac' && !c.post) return b;
      return c;
    });
    next.set(t.team_id, {...t, problems: cells});
    const medal = medalFor(t.rank);   // 实时榜：只看当前名次（区间），不看名单
    const rowStyle = medal ? ' style="--medal-color:' + medal.color + '"' : '';
    const riseCls = up && opt.anim ? (d.follow ? ' rank-rise rank-follow' : ' rank-rise') : '';
    // M17（模板 = 参照榜滚榜页 /roll）：数据行是**上下两栏**——
    //   上栏：名次徽章 | 标志(校徽) | 队伍块；下栏：题数·罚时 | 题目格。
    //   DOM 顺序与视觉顺序一致（列线取证按 DOM 逐列比对）；`?classic=1` 仍是旧顺序与旧单栏结构。
    // 名次块：**只有落在奖项名单里的金/银/铜**才上色（medalFor 就是"按勾的来"的名单口径），
    //   其余名次用朴素样式（数字 + 下方两个小方块）；旧皮肤（?classic=1）仍按名次 topN 走老样式。
    const rankHtml = classicView
      ? '<span class="rank ' + (t.rank <= 3 ? 'top' + t.rank : '') + '">' + (up && opt.anim ? '<span class="arrow">↑</span>' : '') + t.rank + '</span>'
      : '<span class="rank' + (medal ? ' medal-' + medal.index : '') + '">' + (up && opt.anim ? '<span class="arrow">↑</span>' : '') + t.rank + '</span>';
    const logoSrc = '/logo/' + encodeURIComponent(t.team_id);
    // 标志列：走现有 /logo/<team_id> 路由（服务端已按 学号 → 姓名 → 默认校徽 匹配并裁圆）
    const logoHtml = '<span class="row-logo"><img class="logo" src="' + logoSrc + '" data-src="' + logoSrc + '" alt="校徽" onload="logoOk(this)" onerror="logoRetry(this)"></span>';
    // M17 口径微调：滚榜页模板的队伍区**只显示队名**（第二行留空，不放学号/userId/占位符）；
    //   队名原样输出（含 * / ＊ 前缀也不去掉）。旧皮肤（?classic=1）仍保留原来的"队名 + 学号"结构。
    // M39（用户 2026-10-18）：队伍名前的六边形标识 —— 蓝点=正式队伍，黄星=打星队伍。
    //   打星名单由界面「全队标记」里的勾选写入配置（row.star），这里只负责画；
    //   名字里就算带了 * / ＊ 也去掉（用户：去掉吧，留标识）。
    const rawName = String(t.team_name || '');
    const starTeam = !!t.star || /^\s*[*＊]/.test(rawName);
    const cleanName = rawName.replace(/^\s*[*＊]\s*/, '') || rawName;
    const badgeHtml = '<span class="team-badge ' + (starTeam ? 'star' : 'official') + '" title="'
      + (starTeam ? '打星队伍（照常参与排名）' : '正式队伍') + '"><span aria-hidden="true">'
      + (starTeam ? '★' : '') + '</span></span>';
    const teamText = '<div class="team-text"><span class="team-name">' + badgeHtml + '<span class="team-name-text">' + esc(cleanName) + '</span></span></div>';
    const teamHtml = '<div class="team">' + teamText + '</div>';
    // 旧皮肤（?classic=1）里校徽仍在队伍格里，且保留改动前的"队名 + 学号"两行（逐像素对照用）
    const teamHtmlOld = '<div class="team"><img class="logo" src="' + logoSrc + '" data-src="' + logoSrc + '" alt="校徽" onload="logoOk(this)" onerror="logoRetry(this)">' +
      '<div class="team-text"><span class="team-name">' + esc(t.team_name) + '</span><span class="student">' + esc(t.student_number) + '</span></div></div>';
    const solvedHtml = '<span class="numeric solved">' + t.solved + '</span>';
    const penaltyHtml = '<span class="numeric penalty">' + t.penalty + '</span>';
    const cellsHtml = cells.map((c, pi) => { const before=old && old.problems.find(x => x.problem_id === c.problem_id); const changed=before && (before.state!==c.state || before.wrong!==c.wrong || before.time!==c.time || !!before.post!==!!c.post); const allow=animationSlots>0 && (changed || (!before && c.state==='ac')); if (allow) animationSlots--; return rowCellHtml(t, c, old, s, first.has(t.team_id + ':' + c.problem_id), allow); }).join('');
    // M23（学参照榜）：数据行列序 = 名次 | 题数 | 罚时 | 标志 | 队伍 | 题目格（与表头一一对应）。
    //   ?classic=1 仍是旧的单栏结构（名次 队伍 题数 罚时 题目格），逐像素对照用。
    return '<div class="grid board-row' + riseCls + (medal ? ' medal medal-' + medal.index : '') + '"' + rowStyle + ' role="row" data-team="' + esc(t.team_id) + '">' +
      (classicView
        ? rankHtml + teamHtmlOld + solvedHtml + penaltyHtml
        : rankHtml + solvedHtml + penaltyHtml + logoHtml + teamHtml) +
      cellsHtml + '</div>';
  }).join('');
  // M35：名次变了的行，从旧位置滑到新位置（滚榜手感，650ms ease-out）。
  if (flipMs > 0 && flipBefore.size) {
    document.querySelectorAll('#rows .board-row[data-team]').forEach(el => {
      const oldTop = flipBefore.get(el.dataset.team);
      if (oldTop == null) return;
      const d = oldTop - el.getBoundingClientRect().top;
      if (Math.abs(d) < 1) return;
      el.classList.remove('rank-rise', 'rank-follow');   // 避免与 CSS 位移动画打架
      // 用 Web Animations 直接播（比"先写 transform 再 rAF 清空"稳，不受强制重排影响）
      try {
        el.animate([{ transform: 'translateY(' + d.toFixed(1) + 'px)' }, { transform: 'translateY(0)' }],
                   { duration: flipMs, easing: 'cubic-bezier(.22,.61,.36,1)', fill: 'none' });
      } catch (_) {
        el.style.transition = 'transform ' + flipMs + 'ms cubic-bezier(.22,.61,.36,1)';
        el.style.transform = 'translateY(' + d.toFixed(1) + 'px)';
        requestAnimationFrame(() => { el.style.transform = ''; });
      }
    });
  }
  // M41：悬停信息卡要用的原始数据（逐题提交记录 / 题目 id→字母）。
  model.subsByTeam = s.subs_by_team || {};
  model.problemLabels = s.problem_labels || {};
  model.problemList = problems;
  // 罚时口径跟着比赛数据走（默认 20 分钟）
  model.penaltyMinutes = Number((s.contest || {}).penalty_minutes || 20) || 20;
  model.hasPrev = true;   // M30：这一帧之后就算有上一帧了，下一次 AC 才表演滑入
  const count = opt.rows || Math.max(1, list.length);
  // M28（用户 2026-10-17）：人数不满一屏时**不补空行** —— 补了会让内容比视口高，
  //      于是「没占满一页还在来回滑」。名单够长时（list.length >= count）本来也不补。
  //      原实现：if (list.length < count) 补 count-list.length 行 blank 行。
  model.previous = next; model.renderCount++;
  const gap = dataGap(s);
  const countEl = document.getElementById('count');
  countEl.textContent = '题目 ' + problems.length + ' · 进榜 ' + (s.board || []).length + (opt.top ? ' · 前 ' + opt.top + ' 名' : '') + (opt.rows && !opt.scroll ? ' · 显示 ' + Math.min(opt.rows,candidates) + ' 行' : '') +
    (gap.incomplete ? ' · ⚠ 数据可能不全（' + gap.rows + ' / 期望 ' + gap.expected + '）' : '');
  countEl.classList.toggle('warn', gap.incomplete);
  layout();
}
function layout() {
  const opt = model.options, scale = opt.scale || 1;
  const pad = parseFloat(getComputedStyle(document.body).paddingLeft) || 0;
  const width = (innerWidth - pad*2)/scale, height = (innerHeight - pad*2)/scale;
  app.style.width = width + 'px'; app.style.height = height + 'px'; app.style.transform = 'scale(' + scale + ')';
  document.body.classList.toggle('compact', height < 900);
  const topbar = document.querySelector('.topbar').getBoundingClientRect().height/scale;
  const gap = parseFloat(getComputedStyle(app).gap) || 16;
  // M28（用户 2026-10-17 紧急）：一屏行数取「rows 参数」与「实际队伍数」的较小值。
  //   以前 rows=13 但只有 6 支队时，仍按 13 行算行高/视口高度 -> 内容比视口高几十像素，
  //   乒乓滚动就在这几十像素里来回晃（看起来「没满一页还在滑」）。
  const teamsN = model.shown || model.previous.size || 0;
  const count = opt.rows ? (teamsN ? Math.min(opt.rows, teamsN) : opt.rows) : Math.max(1, teamsN || 1);
  // M17：表头/页脚高度改成"量出来的"（参照榜表头 60px、页脚图例常在 1~2 行；旧代码写死 34/28 会裁掉最后一行）
  const boxH = (sel, fallback) => { const el = document.querySelector(sel); return el ? Math.ceil(el.getBoundingClientRect().height/scale) : fallback; };
  const rowsGap = parseFloat(getComputedStyle(document.getElementById('rows')).rowGap) || 8;
  // M23：表格上方的标题栏在正常文档流里，会占高度 —— 必须从可用高度里扣掉（否则最后一行被裁）。
  const titleH = boxH('.rank-titlebar', 0);
  const headH = boxH('.rank-header-row', 34) + titleH, footH = boxH('.rank-footer', 28);
  const available = Math.max(60, height - topbar - gap - headH - footH - rowsGap*2 - 10);
  // M17（模板 = 滚榜页 /roll）：行高按它的 86px 走（上下两栏：上栏名次徽章/标志/队伍，下栏题数罚时/题目格），
  //   一屏放不下就由既有乒乓滚动/自动滚动接管；下限 64 保证两栏不挤扁。
  // M28：人数不满「一屏行数」时把下限放宽到 44，让整页真的能装下 -> 不出现滚动。
  //      正常情况（队伍数 >= 一屏行数）下限仍是 74，视觉与以前完全一致。
  const shrinkFloor = (opt.rows && teamsN && teamsN < opt.rows) ? 44 : 74;
  const rowHeight = Math.min(86, Math.max(shrinkFloor, Math.floor((available-(count-1)*rowsGap)/count)));
  document.documentElement.style.setProperty('--row-height', rowHeight + 'px');
  // 行高够（≥74）走滚榜页的两栏；不够（小屏/队伍多）压缩成单栏，避免裁字
  // 屏幕很矮（<620px）时才退化成单栏，避免两栏被挤扁
  document.body.classList.toggle('roll-compact', rowHeight < 74 || height < 620);
  document.body.classList.toggle('dense', rowHeight < 44);
  // 滚榜页两栏：上栏（名次/标志/队伍）约 58%，下栏（题数·罚时/题目格）吃剩下的（预留上下 padding 8px）
  const bandTop = Math.max(30, Math.round((rowHeight - 8) * 0.58));
  document.documentElement.style.setProperty('--rank-roll-band-top', bandTop + 'px');
  document.documentElement.style.setProperty('--rank-roll-band-bottom', Math.max(20, rowHeight - 8 - bandTop) + 'px');
  // 视口固定为“一屏行数”的高度并允许裁切，行数超过一屏时才有多余量供乒乓滚动；
  // 内容不足一屏时视口收缩到内容高度，不出现空白滚动区。
  const natural = count*rowHeight + (count-1)*rowsGap;
  viewport.style.height = Math.min(natural, available) + 'px';
  if (!scrollable()) viewport.scrollTop = 0;   // M28：不满一页就别留滚动偏移
  // M17：列几何照参照榜 —— 名次/题数/罚时固定宽 + 队伍 1fr + 题目列固定宽（走 --rank-* 变量，密度档自动生效）；
  //      ?classic=1 是旧视觉开关，列几何也保持旧模板（否则 old 皮肤会被新列宽弄花，逐像素对照失效）。
  const nProblem = (model.last && model.last.problems || []).length || 13;
  let columns;
  if (document.body.classList.contains('classic')) {
    const teamWidth = Math.max(155, Math.min(360, width*.23));
    columns = '48px ' + teamWidth + 'px 56px 68px repeat(' + nProblem + ',minmax(0,1fr))';
  } else {
    const teamMin = Math.max(140, Math.min(320, width*.17));
    // M23（学参照榜实时榜单页的列序）：名次 | 题数 | 罚时 | 标志 | 队伍 | 题目列 1fr。
    //   表头与数据行共用这一套 --columns -> 列线中心偏差仍是 0px。
    columns = 'var(--rank-col-width) var(--rank-solve-col-width) var(--rank-penalty-col-width) var(--rank-logo-col-width) minmax(' + Math.round(teamMin) + 'px,var(--rank-team-col-width)) repeat(' + nProblem + ',minmax(0,1fr))';
  }
  document.documentElement.style.setProperty('--columns', columns);
  updateHint();
}
// ===========================================================================
// M23 滚榜时间轴（?timebar=1）
//   参考图：参照榜工具条（时间码 + 可拖动进度条 + 播放键）。
//   数据来源：/api/seek?at=<秒>（服务端按同一 contest 数据重建那一刻的榜单，见 presentation.seek_view）；
//     拖动 -> 设时钟/索引 -> 重渲染，**不刷新页面**。
//   进入 seek（拖动或播放）后暂停 SSE 覆盖（model.timebar.hold），点「实时」恢复跟随。
//   不传 ?timebar=1 时这些函数全部空转，页面行为与以前逐帧一致。
// ===========================================================================
function durationSeconds() {
  const fromLast = Number(model.last && model.last.presentation && model.last.presentation.contest && model.last.presentation.contest.duration_seconds);
  return Number.isFinite(fromLast) && fromLast > 0 ? fromLast : (model.duration || 10800);
}
function updateTimeBar(now) {
  const m = model.timebar;
  if (!m.on) return;
  const dur = durationSeconds();
  // 暂停在某一刻（hold 且不在播放）时用 m.at；实时/播放中用时钟推算的 now。
  const at = now != null ? now : (m.at != null ? m.at : (model.clockBase || 0));
  const clockEl = document.getElementById('timeBarClock');
  if (clockEl) clockEl.textContent = seconds(Math.min(dur, Math.max(0, at)));
  const slider = document.getElementById('timeBarSlider');
  if (slider && !m.dragging) slider.value = String(Math.round(1000 * Math.min(1, Math.max(0, at / (dur || 1)))));
  const play = document.getElementById('timeBarPlay');
  if (play) {
    play.textContent = m.playing ? '❚❚' : '▶';
    play.classList.toggle('is-playing', m.playing);
    play.setAttribute('aria-label', m.playing ? '暂停回放' : '播放回放');
  }
  const live = document.getElementById('timeBarLive');
  if (live) live.classList.toggle('is-live', !m.hold);
}
function fetchSeek(at) {
  const m = model.timebar, token = ++m.token;
  const prevStamp = model.last ? (model.last.last_poll || model.last.updated) : null;
  fetch('/api/seek?at=' + Number(at).toFixed(2) + '&freeze=' + encodeURIComponent(model.options.freeze || ''), { cache: 'no-store' })
    .then(r => r.json())
    .then(data => {
      if (token !== m.token) return;              // 拖动时只认最后一次请求，避免旧帧覆盖新帧
      m.at = Number(data.seek_at != null ? data.seek_at : at);
      render(Object.assign({}, data, { last_poll: data.last_poll || prevStamp, presentation: data.presentation }));
    })
    .catch(() => {});
}
function seekTo(seconds) {
  const m = model.timebar;
  if (!m.on) return;
  const dur = durationSeconds();
  m.hold = true;
  m.at = Math.max(0, Math.min(dur, Number(seconds) || 0));
  updateTimeBar();
  m.pendingAt = m.at;
  clearTimeout(m.throttle);
  const wait = Math.max(0, 80 - (performance.now() - (m.lastReq || 0)));   // 80ms 节流，拖动手感仍即时
  m.throttle = setTimeout(() => { m.lastReq = performance.now(); fetchSeek(m.pendingAt); }, wait);
}
function stopPlay() {
  const m = model.timebar;
  m.playing = false;
  clearInterval(m.timer); m.timer = 0;
  updateTimeBar();
}
function toggleTimebarPlay() {
  const m = model.timebar;
  if (!m.on) return;
  if (m.playing) { stopPlay(); return; }
  if (!m.hold) { m.hold = true; m.at = model.clockBase || 0; }   // 从当前时刻开始播
  m.playing = true;
  m.playFrom = Number(m.at) || 0;
  m.playAt = performance.now();
  clearInterval(m.timer);
  m.timer = setInterval(() => {
    if (!m.playing) return;
    const dur = durationSeconds();
    const at = Math.min(dur, m.playFrom + (performance.now() - m.playAt) / 1000);
    m.at = at;
    if (at >= dur - 0.05) { seekTo(dur); stopPlay(); return; }
    seekTo(at);
  }, 600);
  updateTimeBar();
}
function timebarGoLive() {
  const m = model.timebar;
  stopPlay();
  m.hold = false; m.at = null;
  updateTimeBar();
  const mode = '?freeze=' + encodeURIComponent(model.options.freeze || '');
  Promise.all([fetch('/api/board' + mode).then(r => r.json()), fetch('/api/presentation' + mode).then(r => r.json())])
    .then(([api, presentation]) => render(Object.assign({}, api, { last_poll: api.updated, presentation })))
    .catch(() => {});
}
function initTimeBar() {
  const m = model.timebar;
  if (!m.on) return;
  const slider = document.getElementById('timeBarSlider');
  const play = document.getElementById('timeBarPlay');
  const live = document.getElementById('timeBarLive');
  if (!slider || !play || !live) {              // 没有控件（旧 HTML）就整体关掉，不影响其它功能
    m.on = false; document.body.classList.remove('timebar-on'); return;
  }
  slider.addEventListener('input', () => {
    const cur = model.timebar;
    cur.dragging = true;
    stopPlay();
    cur.hold = true;
    cur.at = Number(slider.value) / 1000 * durationSeconds();
    updateTimeBar();
    seekTo(cur.at);
  });
  const endDrag = () => { model.timebar.dragging = false; };
  slider.addEventListener('change', endDrag);
  slider.addEventListener('pointerup', endDrag);
  play.addEventListener('click', toggleTimebarPlay);
  live.addEventListener('click', timebarGoLive);
  updateTimeBar();
}
// 动效类只在本次播放期间生效；播完即摘掉，页面状态始终与榜单数据一致。
const ANIMATION_CLASSES = ['flash', 'ac-slide', 'wrong-flash', 'pending-pulse', 'rank-rise', 'rank-follow', 'post-flash', 'reveal'];
document.addEventListener('animationend', e => {
  const el = e.target;
  if (el && el.classList) for (const name of ANIMATION_CLASSES) el.classList.remove(name);
});
// M29（用户 2026-10-17）：服务停了以后 OBS 里还挂着最后一帧旧榜单，看着像还在跑。
//   这里做两层反馈：① 顶部指示灯变「重连中」；② 断开超过 2.5 秒就在整页盖一层
//   「实时榜单已停止 · 服务未连接」并把榜单压暗（OBS 里那一眼就能看出是停了，而不是旧数据）。
//   连接恢复（/events 重连成功）自动撤掉。
let offlineSince = 0;
function showOfflineMask(on) {
  document.body.classList.toggle('is-offline', on);
  let el = document.getElementById('offlineMask');
  if (!el) {
    if (!on) return;
    el = document.createElement('div');
    el.id = 'offlineMask';
    el.className = 'offline-mask';
    el.innerHTML = '<div class="offline-card"><b>实时榜单已停止</b>'
                 + '<span>服务未连接，正在等待重连…</span></div>';
    document.body.appendChild(el);
  }
  el.hidden = !on;
}
setInterval(() => { if (offlineSince && Date.now() - offlineSince > 2500) showOfflineMask(true); }, 1000);
function setConnection(ok) {
  document.getElementById('conn').textContent = ok ? '已连接' : '重连中';
  document.getElementById('dot').className = ok ? 'ok' : '';
  if (ok) { offlineSince = 0; showOfflineMask(false); }
  else if (!offlineSince) { offlineSince = Date.now(); }
}
// ---------------------------------------------------------------------------
// M14 数据完整性：一次全量必须拉全
//   `teams` = 榜单行数；`teams_expected` = 数据源里"有提交的队伍数"。二者不等 = 拉取不全
//   （服务端把原因写在 /health 的 fetch_report 里）。页面只在**真的不全**时多打印一句，
//   正常时不改页脚一个字 —— overlay 逐像素回归因此不受影响。
//   强制全量刷新：按钮（控制条里，overlay 下随控制条隐藏）或 `?refresh=full`。
//   服务端只落一个"请求全量"的旗标，由 poller 在下一轮真正执行（重抓全部页 + 队伍名录）。
// ---------------------------------------------------------------------------
function dataGap(s) {
  const rows = (s.board || []).length;
  const expected = Number(s.teams_expected != null ? s.teams_expected : rows);
  const incomplete = s.data_incomplete != null ? !!s.data_incomplete : rows < expected;
  return { rows, expected: Number.isFinite(expected) ? expected : rows, incomplete };
}
function requestFullRefresh() {
  model.refreshAsked = true;
  const el = document.getElementById('ctlFull');
  if (el) { el.disabled = true; el.textContent = '已请求…'; }
  return fetch('/api/refresh-full', { cache: 'no-store' })
    .then(r => r.json())
    .then(j => { if (el) el.textContent = j && j.queued ? '刷新中…' : '刷新失败'; return j; })
    .catch(() => { if (el) el.textContent = '刷新失败'; return null; });
}
async function connect() {
  // 先订阅再读取初始数据；以更新时间过滤网络竞态中的旧结果。
  const modeQuery = '?freeze=' + encodeURIComponent(model.options.freeze);
  const es = new EventSource('/events' + modeQuery);
  let hasEvent = false;
  es.onopen = () => setConnection(true);
  es.addEventListener('update', e => { const s = JSON.parse(e.data); hasEvent = true; if (!window.__ptaBoardIgnoreSSE && !model.timebar.hold) render(s); });
  es.onerror = () => setConnection(false);
  try {
    const [api, presentation] = await Promise.all([fetch('/api/board' + modeQuery).then(r => r.json()), fetch('/api/presentation' + modeQuery).then(r => r.json())]);
    if (!hasEvent) render({...api, last_poll: api.updated, presentation});
  } catch (_) { setConnection(false); }
}
// ---------------------------------------------------------------------------
// M8/M9 控制条：自动滑动开关 + 手动滑动条（人工看用；OBS 叠加 overlay=1 时 display:none）
// 状态优先级：URL 参数 > localStorage（按是否有 overlay 分开存）> 默认值。
// 人工操作（拖滑动条 / 滚轮）会让自动滑动暂停 resume 秒（resume=0 表示不自动恢复）。
//
// M9 空闲自动隐藏：静止时控制条淡出（只改 opacity/visibility，元素仍在布局盒里），
// 滚轮一动 / 指针进入右上角热区 / 进入控制条本身 / 键盘操作 -> 立即淡入；
// 之后连续 ctlhide 秒（默认 4s）无操作再淡出。加载后先显示 ctlhint 毫秒（默认 3s）做提示。
// ctlhide=0 退化成 M8 行为（一直显示）；overlay=1 时任何触发都不显示（body.no-ctl display:none）。
// ---------------------------------------------------------------------------
function readBool(key, def, overlay) {
  if (qs.has(key)) return qs.get(key) === '1';
  try { const v = localStorage.getItem('pta-board-scroll:' + (overlay ? 'overlay' : 'page') + ':' + key); if (v === '0' || v === '1') return v === '1'; } catch (_) {}
  return def;
}
function remember(key, value, overlay) { try { localStorage.setItem('pta-board-scroll:' + (overlay ? 'overlay' : 'page') + ':' + key, value ? '1' : '0'); } catch (_) {} }
const control = { el: null, box: null, slider: null, pos: null, overlay: false, auto: true, visible: true, resume: 10,
                  panel: null, rect: null, hideMs: 4000, hintMs: 3000, hintEnd: 0, on: false, hot: false, hotTimer: 0, hideTimer: 0, gen: 0 };
function initControl() {
  control.overlay = String(option('overlay', 0)) === '1';
  // ctl 默认：正常页显示，overlay=1 不显示；auto 默认 1（保持现状）。
  control.visible = readBool('ctl', !control.overlay, control.overlay);
  control.auto = readBool('auto', true, control.overlay);
  // M14：`?resume=` 传空/非数字时不要退化成 0（0 的语义是"永不自动恢复"，
  // 手滑写成 `?resume=` 会让自动滑动**永久**停在 pausedUntil=Infinity —— 正是"勾了也不动"的另一种成因）。
  const resumeRaw = Number(option('resume', 10));
  control.resume = Math.max(0, Math.min(600, Number.isFinite(resumeRaw) ? resumeRaw : 10));
  // M9：ctlhide=0 -> 一直显示（等于 M8 行为）；>0 -> 静止这么多秒后淡出。
  //     ctlhint=0 关掉"加载后先亮一下"的提示；ctlhint=N（秒）自定义提示时长，默认 3 秒。
  control.hideMs = Math.round(Math.max(0, Math.min(600, number('ctlhide', 4, 0, 600))) * 1000);
  const hintRaw = String(option('ctlhint', '3')).trim();
  control.hintMs = hintRaw === '0' ? 0 : Math.round(Math.max(0.5, Math.min(60, Number(hintRaw) || 3)) * 1000);
  document.body.classList.toggle('no-ctl', !control.visible);
  document.body.classList.toggle('auto-off', !control.auto);
  control.el = document.getElementById('ctl');
  if (!control.el) { control.visible = false; document.body.classList.add('no-ctl'); return; }
  control.box = document.getElementById('ctlAuto');
  control.slider = document.getElementById('ctlSlider');
  control.pos = document.getElementById('ctlPos');
  control.panel = document.querySelector('.board-panel') || control.el.parentElement;
  control.rect = null;
  control.box.checked = control.auto && animOK();
  // M14：anim=0 时勾选框如实显示"未生效"（否则"勾着却不动"= 用户这次的反馈）。
  // 人手勾选 = 明确要动：运行期解除 anim 冻结（model.animBoost），不写回配置。
  document.body.classList.toggle('anim-off', !animOK());
  control.box.addEventListener('change', () => {
    const want = control.box.checked;
    if (want && !model.options.anim) model.animBoost = true;
    if (!want) model.animBoost = false;
    mark(want ? 'toggle-on' : 'toggle-off');
    setAuto(want, true);
  });
  control.full = document.getElementById('ctlFull');
  if (control.full) control.full.addEventListener('click', () => { mark('full-refresh'); requestFullRefresh(); });
  control.slider.addEventListener('input', () => { mark('slider'); setScroll(maxScroll() * (1 - Number(control.slider.value) / 1000), true); });
  // 滚动位置变化（自动滚动/滚轮/拖条都算）时刷新滑动条与位置提示。
  viewport.addEventListener('scroll', syncControl, { passive: true });
  // M10：与滚轮同样非 passive —— 见 onWheel 的说明；本函数只在 max>0 时才改滚动位置。
  viewport.addEventListener('wheel', onWheel, { passive: false });
  // M9：滚轮无论在页面的哪个位置都算"人在操作" -> 立刻显示控制条。
  // 捕获阶段监听，别的 handler 先 preventDefault/stopPropagation 也拦不住。
  window.addEventListener('wheel', onWheelAnywhere, { passive: true, capture: true });
  window.addEventListener('scroll', refreshHotRect, { passive: true, capture: true });
  window.addEventListener('resize', refreshHotRect, { passive: true });
  window.addEventListener('pointermove', onPointerMove, { passive: true });
  control.el.addEventListener('pointerenter', () => { setHot(true); applyState(); showControl(); });
  control.el.addEventListener('pointerleave', () => { setHot(false); applyState(); });
  control.el.addEventListener('focusin', () => { showControl(); touch(); });
  control.el.addEventListener('focusout', () => touch());
  window.addEventListener('keydown', e => {
    const t = e.target, tag = t && t.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || (t && t.isContentEditable)) return;
    if (e.code === 'Space' || e.key === ' ') {
      e.preventDefault();
      mark('space');
      setAuto(!control.auto, true);
      showControl();       // M9：键盘也能把隐藏了的控制条叫回来
      touch();
      return;
    }
    // M10：键盘也能人工翻看（与"空格切换自动滑动"互不冲突）。
    // 先暂停自动滑动（resume 语义同滚轮/拖条），再按固定步长位移：
    // PageUp/PageDown = 一屏、↑/↓ = 一行（行高 + 行间距）、Home/End = 到顶/到底。
    const step = () => {
      const rows = document.getElementById('rows'), kid = rows && rows.children[0];
      const rh = (kid && kid.getBoundingClientRect().height) || 20;
      return Math.max(1, rh + (parseFloat(getComputedStyle(rows).rowGap) || 4));
    };
    let target = null, kind = '';
    if (e.key === 'PageDown') { target = viewport.scrollTop + viewport.clientHeight; kind = 'pagedown'; }
    else if (e.key === 'PageUp') { target = viewport.scrollTop - viewport.clientHeight; kind = 'pageup'; }
    else if (e.key === 'ArrowDown') { target = viewport.scrollTop + step(); kind = 'arrowdown'; }
    else if (e.key === 'ArrowUp') { target = viewport.scrollTop - step(); kind = 'arrowup'; }
    else if (e.key === 'Home') { target = 0; kind = 'home'; }
    else if (e.key === 'End') { target = maxScroll(); kind = 'end'; }
    if (target === null) return;
    e.preventDefault();
    mark(kind);
    showControl();
    setScroll(target, true);   // manual=true -> 暂停自动滑动 resume 秒并同步滑动条/位置提示
    touch();
  });
  if (control.auto) model.scrollAt = -1e9;
  // M9：加载后先亮 ctlhint 毫秒（可发现性），然后按 ctlhide 节奏走。
  control.hintEnd = performance.now() + control.hintMs;
  refreshHotRect();
  showControl();          // 加载后先亮 hintMs 毫秒（ctlhint=0 时立即进入隐藏计时）
  touch();                // 排上 ctlhide 定时器：静止满 ctlhide 秒就淡出
  syncControl();
}
// ---- M9 空闲自动隐藏 -------------------------------------------------------
// 只切 class，不碰 display：隐藏态元素仍占原来的布局盒（面板宽度/列几何不动），
// 但 opacity:0 + visibility:hidden —— 看不见、不接收命中测试、也不挡内容。
// overlay=1（body.no-ctl）时 #ctl 是 display:none，控制条永远不出现。
function writeState() {
  const el = control.el;
  if (!el) return;
  el.classList.toggle('ctl-show', control.on);
  el.classList.toggle('ctl-collapsed', !control.on && !control.overlay);
  el.classList.toggle('ctl-hot', control.hot);
}
function applyState() {
  const el = control.el;
  if (!el) return;
  if (control.overlay) { el.classList.remove('ctl-show'); el.classList.add('ctl-collapsed'); return; }
  writeState();
}
// 取证小记录：滚轮/显示/隐藏的时间戳，验收脚本用它量"触发 -> 可见"的延迟。
function mark(kind, t0) {
  const el = control.el;
  const now = performance.now();
  model.marks.push({ kind, t0: t0 == null ? null : +t0.toFixed(1), t: +now.toFixed(1),
                     on: control.on, hit: el ? el.matches(':hover') : null });
  if (model.marks.length > 120) model.marks.shift();
}
function showControl() {
  if (!control.el || control.overlay || control.on) return;   // overlay 下永远不显示
  control.on = true;
  applyState();
  mark('show');
}
function scheduleHide() {
  if (!control.el || control.overlay || control.hideMs <= 0) return;
  const gen = ++control.gen;
  clearTimeout(control.hideTimer);
  control.hideTimer = setTimeout(() => {
    if (gen !== control.gen || control.overlay) return;
    // 指针还在右上角热区或控制条上：不隐藏，等它离开再计（否则"看不到就没法用"）。
    if (control.hot) { scheduleHide(); return; }
    control.on = false;
    applyState();
    refreshHotRect();   // 收起后热区按面板重新算（收起/展开宽度不同）
    mark('hide');
  }, control.hideMs);
}
// 人工操作：显示控制条 + 重新计时（只有已经显示时才顺延，隐藏态的定时器不用动）。
function touch() { if (control.on) scheduleHide(); }
function onWheelAnywhere(e) {
  mark('wheel', e && e.timeStamp);
  showControl();
  touch();
}
// ---- M9.1 右侧中部热区 ------------------------------------------------------
// 触发区不再是"右上角整列"，而是**贴面板右缘、以面板垂直中线为心的一条带子**：
// 横向 = 展开态宽度 + 6px 余量，纵向 = 面板中间 40% 高度。指针进入即显示控制条，
// 离开先起 420ms 的"冷却"再撤销 hot，避免贴边抖动时反复收起。
function refreshHotRect() {
  const src = control.on ? control.el : (control.panel || control.el);
  const box = src ? src.getBoundingClientRect() : null;
  if (!box) return;
  const bandH = Math.max(120, box.height * 0.4), bandW = Math.max(48, 58 + 6);
  control.rect = { left: box.right - bandW, right: box.right,
                   top: box.top + box.height / 2 - bandH / 2, bottom: box.top + box.height / 2 + bandH / 2 };
}
function setHot(v) {
  clearTimeout(control.hotTimer);
  if (v) { control.hot = true; control.hotTimer = 0; return; }
  control.hotTimer = setTimeout(() => { control.hot = false; applyState(); }, 420);
}
function onPointerMove(e) {
  if (!control.el || control.overlay) return;
  if (!control.rect || !control.on) refreshHotRect();
  const r = control.rect;
  const inHot = !!r && e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom;
  if (inHot !== control.hot) setHot(inHot);
  applyState();
  if (inHot) { showControl(); touch(); }
}
function autoOn() { return control.auto; }
// ---------------------------------------------------------------------------
// M14：把「动效总开关 anim」和「自动滑动」解耦开。
//   修前 `scrollTick` 的条件是 `autoOn() && !autoFrozen() && model.options.anim && ...`：
//   只要 anim=0（发行版 live\config.json 的 view.anim 就是 0）就**永远不滚**，
//   而界面上「自动滑动」照样是可勾的 —— 用户看到的就是「勾了也不动」。
//   现在：
//     * URL/配置里的 anim=0  -> 画面静止（`?auto=1&anim=0` 的既定行为**不回退**）；
//     * 用户**亲手勾选**「自动滑动」 -> `model.animBoost` 在运行期解除冻结
//       （只活在本页内存里，不改 config.json，刷新即恢复）。
//   `__ptaBoard.setAuto()`（取证/脚本用）**不**会 boost —— M11 的
//   `?follow=1&anim=0` 用例仍然必须是"不动"。
function animOK() { return !!model.options.anim || model.animBoost === true; }
// ---------------------------------------------------------------------------
// M32（用户 2026-10-18）：自动滑动改成「定时扫榜」：
//   平时停在第一页不动；每隔 ?sweep=<秒>（默认 180 秒 = 3 分钟）从上往下扫一遍，
//   扫到榜尾停 1.5 秒，再滑回最前面那一页，然后等下一轮。
//   ?sweep=0 关掉（回到老的乒乓滚动，需同时 ?scroll=1）。
// ---------------------------------------------------------------------------
function sweepCfg() {
  if (!model.sweep) {
    const sec = Number(option('sweep', 180));
    model.sweep = { enabled: isFinite(sec) && sec > 0, waitMs: Math.max(1, sec) * 1000,
                    phase: 'idle', until: performance.now() + Math.max(1, sec) * 1000,
                    from: 0, to: 0, at: 0, dur: 0,
                    speed: Math.max(6, Number(option('sweep_speed', 16)) || 16) };   // M34：默认再放慢到 16 像素/秒（≈4.6 秒一行，接近滚榜节奏）
  }
  return model.sweep;
}
// 滑块感：两端慢、中间匀速（smoothstep），整段都是可见的滑动而不是瞬移。
function smoothstep(p) { p = Math.min(1, Math.max(0, p)); return p * p * (3 - 2 * p); }
// M35（用户 2026-10-18）：名次发生变化的行，用滚榜那种滑动挪到新位置（不是瞬间重排）。
//   ?rankmove=<毫秒> 可调，默认 650；0 = 关掉（直接跳）。
function rankMoveMs() { const v = Number(option('rankmove', 900)); return isFinite(v) && v > 0 ? v : 0; }
function sweepDur(px, speed) { return Math.min(180000, Math.max(8000, Math.round(px / speed * 1000))); }
function sweepTick(time) {
  const sw = sweepCfg();
  if (!sw.enabled) return false;
  // M43（用户 2026-10-18）：控制条上关掉「自动滑动」/ 全局冻结时，**一次都不许动**。
  if (!autoOn() || autoFrozen()) {
    if (sw.phase !== 'idle') { sw.phase = 'idle'; sw.until = time + sw.waitMs; viewport.scrollTop = 0; }
    return false;
  }
  if (!scrollable()) return false;
  if (time < model.pausedUntil) return true;
  const max = maxScroll();
  if (sw.phase === 'idle') {
    viewport.scrollTop = 0;
    if (time >= sw.until) {
      sw.from = 0; sw.to = max; sw.at = time; sw.dur = sweepDur(max, sw.speed);
      sw.phase = 'down'; mark('sweep-start');
    }
    return true;
  }
  if (sw.phase === 'down' || sw.phase === 'up') {
    const p = Math.min(1, (time - sw.at) / sw.dur);
    viewport.scrollTop = sw.from + (sw.to - sw.from) * smoothstep(p);
    if (p >= 1) {
      viewport.scrollTop = sw.to;
      if (sw.phase === 'down') { sw.phase = 'hold'; sw.at = time; }
      else { sw.phase = 'idle'; sw.until = time + sw.waitMs; mark('sweep-done'); }
    }
    return true;
  }
  if (sw.phase === 'hold') {
    if (time - sw.at > 3000) {   // M34：榜尾多停 3 秒，最后一页看得清
      sw.phase = 'up'; sw.at = time; sw.from = max; sw.to = 0;
      sw.dur = sweepDur(max, sw.speed);
    }
    return true;
  }
  return false;
}
function acAnimOn() { return String(option('acanim', 1)) !== '0'; }
// M10 取证用：把自动滑动冻结在当前位置（正常使用不会碰它）。取证脚本必须在"滚动位置钉住"
// 之后、截图之前调用它，否则 auto=1 的乒乓会在两次截图之间继续挪画面，跨运行像素比对失效。
function autoFrozen() { return model.scrollFrozen === true; }
function maxScroll() { return Math.max(0, viewport.scrollHeight - viewport.clientHeight); }
function scrollable() {
  // M28：队伍数没到一屏（含空行补位）就完全不动（用户 2026-10-17：不满一页还在滑）。
  // 空数据（还没拉到队伍）时不滚，只在占位空行上抖没有意义。
  if ((model.shown || 0) === 0) return false;
  // M28：内容只比视口高一点点（不足半行）时也不算可滚，避免几十像素来回抖。
  const rh = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--row-height')) || 48;
  return maxScroll() > Math.max(4, rh * 0.5);
}
function setScroll(top, manual) {
  const max = maxScroll();
  viewport.scrollTop = Math.max(0, Math.min(max, top));
  if (manual) touchManual();
  syncControl();
}
function touchManual() {
  // M11：人工一动手就撤销"跟随滚动"（否则用户滚一下又被拉回上升的那一行）。
  model.scrollFollowAbort = true;
  model.scrollPhase = 'hold-top'; model.pendingHold = false; model.scrollAt = performance.now();
  model.pausedUntil = control.resume > 0 ? performance.now() + control.resume * 1000 : Infinity;
}
// M10：滚轮 -> 像素位移。deltaMode：0=像素、1=行（×~16px/行）、2=页（×视口高度）。
// 行/页模式必须换算，否则某些鼠标/触控板"滚一下只动 1px"或"一下跳一屏"。
function wheelDeltaPx(e) {
  const d = Number(e.deltaY) || 0;
  if (e.deltaMode === 1) return d * 16;
  if (e.deltaMode === 2) return d * Math.max(1, viewport.clientHeight);
  return d;
}
// M10：滚轮在**任何组合**下都必须真的滚动。
// 根因（修前）：`#viewport{overflow:hidden}` —— overflow:hidden 的容器浏览器不执行用户滚轮滚动
// （只有 JS 能写 scrollTop），所以"关掉自动滑动就把滚轮交给原生滚动"这条路根本走不通：
// 修前 `if (!autoOn()) return;` 一行直接把滚轮丢掉，用户滚一下完全没反应。
// 现在统一由这里接管：先暂停自动滑动（沿用 resume 语义）再位移，clamp 到 [0, maxScroll]。
// 该监听器不再是 passive：overflow:hidden 下 native 本就不滚，preventDefault 只是保险
// （万一以后放开滚动，避免"原生 + 本函数"一次滚两倍）。
function onWheel(e) {
  // M9：滚轮先"叫出"控制条（window 捕获阶段已经记过一笔），再看怎么滚。
  showControl();
  touch();
  touchManual();
  const max = maxScroll();
  if (max <= 0) return;
  if (e.cancelable) e.preventDefault();
  viewport.scrollTop = Math.max(0, Math.min(max, viewport.scrollTop + wheelDeltaPx(e)));
  syncControl();
}
function setAuto(on, remember_) {
  control.auto = !!on;
  if (!control.auto) model.animBoost = false;      // M14：关掉自动滑动 = 连"运行期解除 anim 冻结"一起收回
  document.body.classList.toggle('auto-off', !control.auto);
  if (control.box) control.box.checked = control.auto && animOK();
  document.body.classList.toggle('anim-off', !animOK());
  if (control.auto) {
    // 从当前位置恢复乒乓：先按原语义停下来停一拍，再往下走。
    model.scrollPhase = 'hold-top'; model.scrollAt = performance.now(); model.pausedUntil = 0;
  } else {
    model.pausedUntil = 0;
    model.pendingHold = false;                 // M11：关掉自动滑动后不许"停留计时"残留
    if (model.scrollPhase === 'follow') model.scrollPhase = 'hold-top';
    // 关掉自动滑动后不留动画残留：摘掉所有动效类，画面完全静止。
    for (const el of viewport.querySelectorAll('.' + ANIMATION_CLASSES.join(',.'))) for (const n of ANIMATION_CLASSES) el.classList.remove(n);
  }
  if (remember_) remember('auto', control.auto, control.overlay);
  touch();
  layout();
}
function syncControl() {
  if (control.slider) {
    const max = maxScroll();
    control.slider.value = String(max > 0 ? Math.round(1000 * (1 - Math.min(max, viewport.scrollTop) / max)) : 0);
  }
  const now = performance.now();
  if (now - model.hintThrottle < 120) return;
  model.hintThrottle = now;
  updateHint();
}
function updateHint() {
  if (!control.pos) return;
  const kids = document.getElementById('rows').children;
  if (!kids.length || !model.last) { control.pos.textContent = '—'; return; }
  const total = kids.length, rh = kids[0].getBoundingClientRect().height || 20;
  const step = rh + (parseFloat(getComputedStyle(document.getElementById('rows')).rowGap) || 4);
  const max = maxScroll(), seen = Math.max(1, Math.round(viewport.clientHeight / step));
  const first = Math.min(Math.max(1, Math.floor(viewport.scrollTop / step) + 1), Math.max(1, total - seen + 1));
  const s = first + '–' + Math.min(total, first + seen - 1) + ' / ' + total;
  if (s !== model.lastHint) { model.lastHint = s; control.pos.textContent = s; }
}
function scrollTick(time) {
  // 自动滑动的相位推进（hold-top → down → hold-bottom → up，乒乓来回），语义与 M6/M7 一致。
  // M10：anim=0 表示"自动滑动冻结" —— 这里什么都不做。
  // 修前这里是 `viewport.scrollTop = 0`：每帧强制归零，于是 `auto=1&anim=0` 下用户滚轮刚滚
  // 就被弹回顶部（表现为"滚不动"）。人工滚动（滚轮/滑动条/键盘）在任何组合下都必须有效，
  // 且不被回写覆盖 —— pausedUntil 期间（人工刚操作过）本来就不进这个分支，双重保险。
  // M14：`anim=0` 不再单独成为自动滑动的闸门 —— 见 animOK()。
  // M32：定时扫榜优先（返回 true 就独占这一帧）。
  if (sweepTick(time)) { syncControl(); requestAnimationFrame(scrollTick); return; }
  // M31：处于「跟随」阶段时优先把这一段时间轴走完 —— 即使 anim=0（画面静止）也要滑过去。
  if (model.scrollPhase === 'follow' && time >= model.pausedUntil) {
    followTick(time);
    syncControl();
    requestAnimationFrame(scrollTick);
    return;
  }
  if (autoOn() && animOK() && !autoFrozen() && scrollable() && time >= model.pausedUntil) {
    const max = maxScroll();
    if (model.pendingHold && time - model.scrollAt > model.options.followPause * 1000) {
      // M11：跟随停留 follow_pause 秒 → 回到乒乓（从当前位置继续往下走）。
      model.pendingHold = false; model.scrollPhase = 'down'; model.scrollAt = time; model.scrollStart = viewport.scrollTop;
    }
    else if (model.scrollPhase === 'hold-top' && time - model.scrollAt > model.options.scrollPause * 1000) { model.scrollPhase = 'down'; model.scrollAt = time; model.scrollStart = viewport.scrollTop; }
    else if (model.scrollPhase === 'down') { viewport.scrollTop = Math.min(max, model.scrollStart + (time - model.scrollAt) * model.options.scrollSpeed / 1000); if (viewport.scrollTop >= max - 1) { model.scrollPhase = 'hold-bottom'; model.scrollAt = time; } }
    else if (model.scrollPhase === 'hold-bottom' && time - model.scrollAt > model.options.scrollPause * 1000) { model.scrollPhase = 'up'; model.scrollAt = time; model.scrollStart = viewport.scrollTop; }
    else if (model.scrollPhase === 'up') { viewport.scrollTop = Math.max(0, model.scrollStart - (time - model.scrollAt) * model.options.scrollSpeed / 1000); if (viewport.scrollTop <= 1) { model.scrollPhase = 'hold-top'; model.scrollAt = time; } }
  }
  syncControl();
  requestAnimationFrame(scrollTick);
}
window.addEventListener('resize', layout);
setInterval(updateClock, 500);
requestAnimationFrame(scrollTick);
// 验收/取证用的小接口：把"比赛进行"时钟钉在固定值，便于两次截图逐像素比对
// （时钟每秒跳一下会让整页 sha256 永远对不上）。正常使用不会碰它。
window.__ptaBoard = {
  freezeClock(v) { model.clockFrozen = v ? 1000 : null; model.receivedAt = 1000; updateClock(); },
  // 把自动滑动钉住（取证用）：on 时 scrollTick 一步都不推进，人工滚动照常有效。
  freezeScroll(v) { model.scrollFrozen = v !== false; },
  state: () => ({ auto: control.auto, overlay: control.overlay, ctlVisible: control.visible, resume: control.resume,
                  scrollTop: viewport.scrollTop, maxScroll: maxScroll(), phase: model.scrollPhase,
                  pausedUntil: model.pausedUntil, autoFrozen: autoFrozen(),
                  // M14：anim/自动滑动解耦后的实际状态（勾选是否真的会动，看 effectiveAuto）
                  animBoost: model.animBoost, animOK: animOK(), effectiveAuto: autoOn() && animOK(),
                  refreshAsked: model.refreshAsked,
                  ctlChecked: control.box ? control.box.checked : null,
                  // M11：跟随参数与状态
                  follow: model.options.follow, followPause: model.options.followPause, followMax: model.options.followMax,
                  pendingHold: model.pendingHold, followQueueLen: model.followQueue.length,
                  frozen: !!(model.last && model.last.frozen), anim: model.options.anim,
                  // M9：空闲隐藏的实时状态
                  ctlOn: control.on, hideMs: control.hideMs, hintMs: control.hintMs, hot: control.hot,
                  ctlShown: control.el ? getComputedStyle(control.el).visibility === 'visible' && Number(getComputedStyle(control.el).opacity) > 0.5 : false,
                  cls: control.el ? control.el.className : null }),
  marks: () => model.marks.slice(),
  clearMarks: () => { model.marks.length = 0; },
  poke: () => { showControl(); touch(); },
  // ---- M11 取证接口（验收脚本用；不参与渲染） -------------------------------
  // 「检测到 rank 变化 → 画面位置」的数字：目标行、目标 scrollTop、实际 scrollTop。
  follow: () => model.followPlan ? {...model.followPlan,
    actualScrollTop: viewport.scrollTop,
    scrollTopNow: +viewport.scrollTop.toFixed(2),
    landed: Math.abs(viewport.scrollTop - model.followPlan.target) < 2,
    rowVisible: (() => {
      const row = document.querySelector('.board-row[data-team="' + model.followPlan.team + '"]');
      if (!row) return null;
      const rr = row.getBoundingClientRect(), vv = viewport.getBoundingClientRect();
      return { rowTop: +rr.top.toFixed(1), rowBottom: +rr.bottom.toFixed(1),
               viewTop: +vv.top.toFixed(1), viewBottom: +vv.bottom.toFixed(1),
               fullyVisible: rr.top >= vv.top - 1 && rr.bottom <= vv.bottom + 1 };
    })() } : null,
  followQueue: () => model.followQueue.map(q => ({ id: q.id, delta: q.delta, from: q.from, to: q.to })),
  followErrors: () => model.followErrors.slice(),
  clearFollow: () => { model.followPlan = null; model.followErrors.length = 0; model.followQueue.length = 0; model.followTimes.length = 0; },
  // 题格取证：返回某队某题的 class / data-post / 段数与文本（闪烁/黄格/红黄并排都用它取数）。
  cell: (team, problemIndex) => {
    const row = document.querySelector('.board-row[data-team="' + team + '"]');
    if (!row) return null;
    const cells = row.querySelectorAll('.cell');
    const el = cells[problemIndex]; if (!el) return null;
    const cs = getComputedStyle(el);
    return { cls: el.className, state: el.dataset.state, post: el.dataset.post,
             bg: cs.backgroundColor, bgImage: cs.backgroundImage, color: cs.color,
             anim: cs.animationName, animDuration: cs.animationDuration,
             label: el.querySelector('.cell-label') ? el.querySelector('.cell-label').textContent : '',
             segs: [...el.querySelectorAll('.cell-seg')].map(s => ({ state: s.dataset.seg, label: s.querySelector('.cell-label') ? s.querySelector('.cell-label').textContent : '',
               bg: getComputedStyle(s).backgroundColor, cls: s.className })) };
  },
  // 冻结动画相位（截图逐像素比对用）：把整页动画钉在 0s 相位。
  pauseAnimations: (on) => { for (const a of document.getAnimations()) { try { if (on !== false) { a.currentTime = 0; a.pause(); } else a.play(); } catch (_) {} } },
  scanBlink: () => [...document.querySelectorAll('.blink')].map(el => ({ team: el.closest('.board-row')?.dataset.team,
    bg: getComputedStyle(el).backgroundColor, anim: getComputedStyle(el).animationName })),
  // 取证用：脚本注入两帧数据，检查"rank 变化 → 画面位置"的因果（正常使用不会碰它）。
  render: (state) => render(state),
  setAuto: (on) => setAuto(on, false),
  setScroll: (top, manual) => setScroll(top, manual !== false),
  ignoreEvents: () => { window.__ptaBoardIgnoreSSE = true; },
  // M23 时间轴取证接口（?timebar=1）：当前状态 / 跳到某秒 / 播放暂停 / 回到实时。
  timebar: () => ({ on: model.timebar.on, hold: model.timebar.hold,
                    at: model.timebar.at == null ? null : +Number(model.timebar.at).toFixed(2),
                    playing: model.timebar.playing, duration: durationSeconds(),
                    clock: (document.getElementById('timeBarClock') || {}).textContent || null,
                    slider: (document.getElementById('timeBarSlider') || {}).value || null }),
  seek: (s) => seekTo(s),
  playTimebar: () => toggleTimebarPlay(),
  goLive: () => timebarGoLive(),

};
fetch('/api/config').then(r => r.json()).then(configure).catch(() => configure({})).then(initControl).then(initTimeBar)
  .then(() => { if (String(option('refresh', '')).toLowerCase() === 'full') return requestFullRefresh(); })
  .then(connect);

// =====================================================================
// M41（用户 2026-10-18）：悬停信息卡（照参照榜 roll 页面那套）
//   · 悬停队伍名 -> 队伍信息卡（队名 / 学号 / 正式 or 打星 / 解题 / 罚时）
//   · 悬停题目格 -> 题目卡（状态 / 提交次数 / 用时 / 罚时算式 / 队伍 / 逐条提交）
//   · 悬停「解题数」-> 解题数：N ；悬停「罚时」-> 罚时：N 分钟（h:mm:ss）
//   只读展示：不改任何数据、不影响排名与发奖。overlay（OBS）下不显示。
// =====================================================================
const VERDICT = {
  ACCEPTED: ['答案正确', 'ac'],
  PENDING: ['待判', 'pend'],
  POST_FREEZE: ['待判', 'pend'],
  WRONG_ANSWER: ['答案错误', 'wa'],
  COMPILE_ERROR: ['编译错误', 'ce'],
  NON_ZERO_EXIT_CODE: ['运行错误', 're'],
  SEGMENTATION_FAULT: ['段错误', 're'],
  TIME_LIMIT_EXCEEDED: ['运行超时', 'tle'],
  MEMORY_LIMIT_EXCEEDED: ['内存超限', 'mle'],
  PRESENTATION_ERROR: ['格式错误', 'pe'],
  OUTPUT_LIMIT_EXCEEDED: ['输出超限', 'ole'],
};
const LANG = { GCC: 'C', GXX: 'C++', PYTHON3: 'Python3', PYTHON2: 'Python2', JAVAC: 'Java', JAVA: 'Java' };
function verdictOf(code) { const v = VERDICT[String(code || '').toUpperCase()]; return v ? { text: v[0], cls: v[1] } : { text: String(code || '未知'), cls: 'other' }; }
function langOf(code) { const k = String(code || '').toUpperCase(); return LANG[k] || k || '—'; }
function mmss(sec) {
  const t = Math.max(0, Math.round(Number(sec) || 0));
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  const p2 = n => (n < 10 ? '0' + n : '' + n);
  return h ? (h + ':' + p2(m) + ':' + p2(s)) : (p2(m) + ':' + p2(s));
}
function tipEl() {
  let el = document.getElementById('tipcard');
  if (!el) {
    el = document.createElement('div');
    el.id = 'tipcard';
    el.className = 'tipcard';
    document.body.appendChild(el);
  }
  return el;
}
function hideTip() { const el = document.getElementById('tipcard'); if (el) el.classList.remove('show'); }
function placeTip(ev) {
  const el = tipEl();
  const w = el.offsetWidth || 260, h = el.offsetHeight || 120, pad = 16;
  let x = ev.clientX + pad, y = ev.clientY + pad;
  if (x + w > innerWidth - 10) x = Math.max(10, ev.clientX - w - pad);
  if (y + h > innerHeight - 10) y = Math.max(10, innerHeight - h - 10);
  el.style.left = Math.round(x) + 'px';
  el.style.top = Math.round(y) + 'px';
}
function showTip(html, ev) {
  if (document.body.classList.contains('overlay')) return;
  const el = tipEl();
  el.innerHTML = html;
  el.classList.add('show');
  placeTip(ev);
}
function esc2(v) { return String(v == null ? '' : v).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
function tipRow(k, v) { return '<div class="tip-row"><span class="tip-k">' + esc2(k) + '</span><span class="tip-v">' + v + '</span></div>'; }
function teamOf(id) {
  const list = (model.last && model.last.board) || [];
  return list.find(t => String(t.team_id) === String(id)) || null;
}
function teamTipHtml(t) {
  const star = !!t.star;
  const badge = star ? '打星队伍' : '正式队伍';
  return '<div class="tip-head"><b>队伍信息</b><span class="tip-tag ' + (star ? 'star' : 'official') + '">'
    + badge + '</span></div>'
    + tipRow('队伍', '<b>' + esc2(t.team_name) + '</b>')
    + tipRow('学号', esc2(t.student_number || '—'))
    + tipRow('名次', String(t.rank || '—'))
    + tipRow('解题', String(t.solved) + ' 题')
    + tipRow('罚时', String(t.penalty) + ' 分钟（' + mmss(t.penalty * 60) + '）');
}
function cellTipHtml(row, cell, pi) {
  const problems = model.problemList || [];
  const p = problems[pi] || {};
  const label = p.label || (model.problemLabels || {})[String(p.id)] || '?';
  const cls = cell.classList;
  let state = '未提交', stateCls = 'none';
  if (cls.contains('ac')) { state = '已通过'; stateCls = 'ac'; }
  else if (cls.contains('wrong')) { state = '未通过'; stateCls = 'wa'; }
  else if (cls.contains('pending-cell')) { state = '判题中'; stateCls = 'pending'; }
  else if (cls.contains('has-post')) { state = '封榜后提交'; stateCls = 'post'; }
  else if (cls.contains('pending')) { state = '未提交'; stateCls = 'none'; }
  let all = ((model.subsByTeam || {})[String(row.team_id)] || []).filter(x => String(x[0]) === String(p.id));
  // M44：通过之后的所有提交都不做数 —— 列表/次数/罚时一律截断在「第一次 AC」那一刻。
  const acIdx = all.findIndex(x => String(x[2]).toUpperCase() === 'ACCEPTED');
  if (acIdx >= 0) all = all.slice(0, acIdx + 1);
  // M43：封榜后的提交照常列出（时间/语言都在），判定由服务端写成 PENDING -> 显示「待判」。
  const isPend = x => { const v = String(x[2]).toUpperCase(); return v === 'PENDING' || v === 'POST_FREEZE' || Number(x[1]) < 0; };
  const postN = all.filter(isPend).length;
  const subs = all.filter(x => !isPend(x) && Number(x[1]) >= 0);
  const acSub = subs.filter(x => String(x[2]).toUpperCase() === 'ACCEPTED');
  // M47：编译错误（COMPILE_ERROR）不算罚时、也不算次数 —— 罚时算式里的错次同样要把它排除。
  const NO_PENALTY = { ACCEPTED: 1, COMPILE_ERROR: 1 };
  const wrongBefore = acSub.length
    ? subs.filter(x => x[1] < acSub[0][1] && !NO_PENALTY[String(x[2]).toUpperCase()]).length
    : 0;
  const pen = Number(model.penaltyMinutes || 20);
  let penaltyText = '—', timeText = '—';
  if (acSub.length) {
    const sec = Number(acSub[0][1]) || 0;
    timeText = mmss(sec);
    // M56（用户 2026-10-18 指定口径②）：这里只显示「错误次数 × 罚时分钟」本身的罚时，
    //   不再把通过用时算进去（通过用时在上面的「用时」列单独显示，名次仍是 ICPC 口径）。
    penaltyText = '20*' + wrongBefore + '=' + (wrongBefore * pen);
  } else if (all.length) {
    timeText = mmss(all[all.length - 1][1]);   // 没通过就显示最后一次提交的时刻（封榜后的也算，时间不敏感）
  }
  // M47（用户 2026-10-18）：这里显示的「N 次提交」= 记录条数 **减去编译错误**（CE 不算罚时、
  //   也不算次数），这样和格子上的标记、罚时算式都对得上；记录列表本身仍把 CE 列出来。
  const ceCount = all.filter(x => String(x[2]).toUpperCase() === 'COMPILE_ERROR').length;
  const countN = Math.max(0, all.length - ceCount);
  let html = '<div class="tip-head tip-head-cell"><b>题目 ' + esc2(label) + '</b>'
    + '<span class="tip-state ' + stateCls + '">' + state + '</span>'
    + '<span class="tip-dim">' + countN + ' 次提交</span>'
    + '<span class="tip-dim">' + timeText + '</span>'
    + '<span class="tip-mono">' + penaltyText + '</span></div>'
    + tipRow('队伍', '<b>' + esc2(row.team_name) + '</b>')
    + tipRow('学号', esc2(row.student_number || '—'));
  // M46（用户 2026-10-18）：**提交记录照常列出来**，只是「第一次 AC 之后」的那些不再显示
  //   （`all` 已在上面按 AC 截断），封榜后的条目判定显示「待判」。
  if (all.length) {
    const total = all.length;
    html += '<div class="tip-subs">' + all.slice().reverse().map((x, i2) => {
      const pend = isPend(x);
      const v = verdictOf(pend ? 'PENDING' : x[2]);
      const secs = Number(x[1]) >= 0 ? x[1] : null;
      return '<div class="tip-sub' + (pend ? ' tip-sub-pend' : '') + '"><span class="tip-no">#' + (total - i2) + '</span>'
        + '<span class="tip-t">' + (secs === null ? '—' : mmss(secs)) + '</span>'
        + '<span class="tip-vd ' + v.cls + '">' + v.text + '</span>'
        + '<span class="tip-lang">' + langOf(x[3]) + '</span></div>';
    }).join('') + '</div>';
  } else {
    html += '<div class="tip-empty">暂无提交</div>';
  }
  return html;
}
function tipFor(ev) {
  if (document.body.classList.contains('overlay')) return null;
  const t = ev.target;
  if (!(t instanceof Element)) return null;
  const nameEl = t.closest('.team-name');
  const rowEl = t.closest('.board-row');
  if (!rowEl) return null;
  const row = teamOf(rowEl.dataset.team) || (model.last && (model.last.board || []).find(x => String(x.team_id) === rowEl.dataset.team));
  const solvedEl = t.closest('.numeric.solved');
  const penEl = t.closest('.numeric.penalty');
  if (solvedEl && row) return '<div class="tip-mini">解题数：<b>' + row.solved + '</b></div>';
  if (penEl && row) return '<div class="tip-mini">罚时：<b>' + row.penalty + '</b> 分钟（<span class="tip-mono">' + mmss(row.penalty * 60) + '</span>）</div>';
  if (nameEl && row) return teamTipHtml(row);
  const cell = t.closest('.cell');
  if (cell && row) {
    const pi = parseInt(cell.style.getPropertyValue('--pi'), 10);
    return cellTipHtml(row, cell, Number.isFinite(pi) ? pi : 0);
  }
  return null;
}
function bindTips() {
  const rows = document.getElementById('rows');
  if (!rows || rows.__tipsBound) return;
  rows.__tipsBound = true;
  rows.addEventListener('mouseover', ev => {
    const html = tipFor(ev);
    if (html) showTip(html, ev); else hideTip();
  });
  rows.addEventListener('mousemove', ev => {
    const el = document.getElementById('tipcard');
    if (el && el.classList.contains('show')) placeTip(ev);
  });
  rows.addEventListener('mouseleave', hideTip);
  const vp = document.getElementById('viewport');
  if (vp) vp.addEventListener('scroll', hideTip, { passive: true });
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bindTips);
else bindTips();
setInterval(bindTips, 1500);
