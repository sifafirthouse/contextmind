/* ContextMind Website - runs fully in your browser (phone or laptop). Data is saved on this device only. */
(function () {
'use strict';

// ===== 1. STORAGE ===========================================================
var LS_KEY = 'contextmind_web_v1', PW_KEY = 'contextmind_pw_v1';
var DEFAULTS = { sessions: [], breaks: [], currentId: null, lastSeen: Date.now(),
  settings: { breakSec: 60, shortMin: 2, endMin: 10, autoLockMin: 3 } };
function clone(o) { return JSON.parse(JSON.stringify(o)); }
function load() {
  try {
    var d = JSON.parse(localStorage.getItem(LS_KEY));
    if (d) { var base = clone(DEFAULTS); d.settings = Object.assign(base.settings, d.settings || {}); return Object.assign(base, d); }
  } catch (e) {}
  return clone(DEFAULTS);
}
var data = load();
function save() { try { localStorage.setItem(LS_KEY, JSON.stringify(data)); } catch (e) {} }

// ===== 2. HELPERS ===========================================================
function $(id) { return document.getElementById(id); }
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
function fmt(ts) { var d = new Date(ts); return d.toLocaleDateString([], { day: '2-digit', month: 'short' }) + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); }
function toast(msg) { var t = $('toast'); t.textContent = msg; t.style.display = 'block'; clearTimeout(toast.h); toast.h = setTimeout(function () { t.style.display = 'none'; }, 4500); }
function normUrl(u) { u = u.trim(); return /^https?:\/\//i.test(u) ? u : 'https://' + u; }
function cur() { return data.sessions.filter(function (s) { return s.id === data.currentId; })[0] || null; }
function byId(id) { return data.sessions.filter(function (s) { return s.id === id; })[0] || null; }

// ===== 3. PASSWORD LOCK =====================================================
function weakHash(t) { var h = 5381; for (var i = 0; i < t.length; i++) h = ((h << 5) + h + t.charCodeAt(i)) | 0; return 'w' + h; }
function sha(text) {
  if (window.crypto && crypto.subtle) {
    return crypto.subtle.digest('SHA-256', new TextEncoder().encode(text)).then(function (b) {
      return Array.from(new Uint8Array(b)).map(function (x) { return x.toString(16).padStart(2, '0'); }).join('');
    });
  }
  return Promise.resolve(weakHash(text));
}
function getPw() { try { return JSON.parse(localStorage.getItem(PW_KEY)); } catch (e) { return null; } }
function setPw(pw) {
  var salt = Math.random().toString(36).slice(2) + Date.now();
  return sha(salt + pw).then(function (h) { localStorage.setItem(PW_KEY, JSON.stringify({ salt: salt, hash: h })); });
}
function checkPw(pw) { var p = getPw(); if (!p) return Promise.resolve(false); return sha(p.salt + pw).then(function (h) { return h === p.hash; }); }

var unlocked = false, pending = null;
function showLock() {
  unlocked = false; $('app').innerHTML = ''; $('app').style.display = 'none'; $('lock').style.display = 'block';
  $('pw').value = ''; $('lockErr').textContent = '';
  $('lockMsg').textContent = getPw() ? 'Locked. Enter password to see your work.' : 'First time: create a password for this device.';
  $('unlockBtn').textContent = getPw() ? 'Unlock' : 'Create password';
  $('welcomeBanner').style.display = pending ? 'block' : 'none';
  document.title = pending ? '(!) Welcome back - ContextMind' : 'ContextMind';
}
function unlockNow() { unlocked = true; $('lock').style.display = 'none'; $('app').style.display = 'block'; lastActive = Date.now(); render(); }
$('unlockBtn').onclick = function () {
  var pw = $('pw').value;
  if (!getPw()) {
    if (pw.length < 4) { $('lockErr').textContent = 'Use at least 4 characters.'; return; }
    setPw(pw).then(unlockNow); return;
  }
  checkPw(pw).then(function (ok) { if (ok) unlockNow(); else $('lockErr').textContent = 'Wrong password'; });
};
$('pw').addEventListener('keydown', function (e) { if (e.key === 'Enter') $('unlockBtn').click(); });

// ===== 4. SESSIONS & ITEMS ==================================================
function endCurrent(ts) { var s = cur(); if (s && !s.end) s.end = Math.max(ts || Date.now(), s.start); data.currentId = null; }
function newSession(title) {
  endCurrent();
  var s = { id: 's' + Date.now(), title: (title || '').trim() || 'Untitled session', start: Date.now(), end: null, items: [], stopped: '', summary: '' };
  data.sessions.unshift(s); data.currentId = s.id; save(); render();
}
function addItem(type, text) {
  var s = cur(); text = (text || '').trim(); if (!s || !text) return;
  s.items.push({ id: 'i' + Date.now() + Math.random().toString(36).slice(2, 5), type: type, text: text, done: false, ts: Date.now() });
  save(); render();
}
function resumeSession(id) {
  var s = byId(id); if (!s) return;
  if (cur() && cur().id !== id) endCurrent();
  s.end = null; data.currentId = id; pending = null; document.title = 'ContextMind'; save();
  var links = s.items.filter(function (i) { return i.type === 'link'; }), blocked = 0;
  links.forEach(function (l) { var w = window.open(normUrl(l.text), '_blank'); if (!w) blocked++; });
  render();
  toast(links.length ? (blocked ? 'Browser blocked some pop-ups. Tap the links below to open them.' : 'Resumed: ' + links.length + ' link(s) opened.') : 'Resumed. Your notes and next step are below.');
  setTimeout(function () { var c = $('current'); if (c) c.scrollIntoView({ behavior: 'smooth' }); }, 100);
}

// ===== 5. SMART ANALYSIS: TF-IDF + COSINE SIMILARITY (same idea as the laptop version) =====
var STOP = 'a an and are as at be but by for from has have in is it its of on or that the this to was were will with you your i my me we our http https www com org net html'.split(' ');
function tokens(t) { return (t.toLowerCase().match(/[a-z0-9]{2,}/g) || []).filter(function (w) { return STOP.indexOf(w) < 0; }); }
function sessionText(s) { return s.title + ' ' + s.items.map(function (i) { return i.text; }).join(' ') + ' ' + (s.stopped || ''); }
function tfidfVectors(docs) {
  var toks = docs.map(tokens), df = {}, n = docs.length;
  toks.forEach(function (ts) { var seen = {}; ts.forEach(function (w) { if (!seen[w]) { seen[w] = 1; df[w] = (df[w] || 0) + 1; } }); });
  return toks.map(function (ts) {
    var tf = {}; ts.forEach(function (w) { tf[w] = (tf[w] || 0) + 1; });
    var v = {}; Object.keys(tf).forEach(function (w) { v[w] = (tf[w] / ts.length) * (Math.log((1 + n) / (1 + df[w])) + 1); });
    return v;
  });
}
function cosine(a, b) {
  var dot = 0, na = 0, nb = 0, k;
  for (k in a) { na += a[k] * a[k]; if (b[k]) dot += a[k] * b[k]; }
  for (k in b) nb += b[k] * b[k];
  return na && nb ? dot / Math.sqrt(na * nb) : 0;
}
function similarTo(sid) {
  var all = data.sessions, idx = all.map(function (s) { return s.id; }).indexOf(sid);
  if (idx < 0 || all.length < 2) return [];
  var vecs = tfidfVectors(all.map(sessionText));
  return all.map(function (s, i) { return { s: s, score: cosine(vecs[idx], vecs[i]) }; })
    .filter(function (r) { return r.s.id !== sid; }).sort(function (a, b) { return b.score - a.score; }).slice(0, 3);
}
function smartSummary(s) {
  var vecs = tfidfVectors(data.sessions.map(sessionText)), i = data.sessions.indexOf(s), v = vecs[i] || {};
  var keys = Object.keys(v).sort(function (a, b) { return v[b] - v[a]; }).slice(0, 5);
  var notes = s.items.filter(function (x) { return x.type === 'note'; }).length, links = s.items.filter(function (x) { return x.type === 'link'; }).length;
  var tasks = s.items.filter(function (x) { return x.type === 'task'; }), done = tasks.filter(function (x) { return x.done; }).length;
  return 'Worked on: ' + (keys.join(', ') || 'no keywords yet') + '. ' + notes + ' note(s), ' + links + ' link(s), ' + done + '/' + tasks.length + ' task(s) done.';
}

// ===== 6. BREAK DETECTION (idle + tab switch + closing the page / switching the device off) =====
var lastActive = Date.now(), away = false, awayFrom = 0, frozen = false;
function classify(min) { var st = data.settings; return min < st.shortMin ? 'Short Interruption' : (min < st.endMin ? 'Real Break' : 'End of Session'); }
function handleReturn(gapMs, fromTs) {
  var minutes = Math.round(gapMs / 6000) / 10, type = classify(minutes);
  var s = cur() || data.sessions[0] || null;
  data.breaks.unshift({ ts: fromTs, sid: s ? s.id : null, title: s ? s.title : '', minutes: minutes, type: type });
  if (type === 'End of Session' && cur()) endCurrent(fromTs);
  if (s) pending = { sid: s.id, minutes: minutes, type: type };
  data.lastSeen = Date.now(); lastActive = Date.now(); away = false; save();
  showLock();                                   // blur everything until the password is entered
}
function onActivity() {
  var now = Date.now();
  if (away) { handleReturn(now - awayFrom, awayFrom); return; }
  lastActive = now;
}
['mousemove', 'mousedown', 'keydown', 'touchstart', 'scroll', 'click'].forEach(function (e) { document.addEventListener(e, onActivity, { passive: true }); });
setInterval(function () {
  var now = Date.now(), st = data.settings;
  if (!away && now - lastActive >= st.breakSec * 1000 && data.sessions.length) { away = true; awayFrom = lastActive; }
  if (!away && !frozen && !document.hidden) { data.lastSeen = lastActive; save(); }
  if (unlocked && !away && now - lastActive >= st.autoLockMin * 60000) showLock();       // auto-lock
  var el = $('elapsed'), s = cur();
  if (el && s) el.textContent = Math.max(0, Math.round((now - s.start) / 60000)) + ' min since start';
  var idleEl = $('idle'); if (idleEl) idleEl.textContent = Math.round((now - lastActive) / 1000);
}, 2000);
document.addEventListener('visibilitychange', function () {
  if (document.hidden) { if (!frozen && !away) { data.lastSeen = lastActive; save(); } return; }
  var gap = Date.now() - data.lastSeen;
  if (data.sessions.length && gap >= data.settings.breakSec * 1000 && !away) handleReturn(gap, data.lastSeen);
  else lastActive = Date.now();
});
window.addEventListener('pagehide', function () { if (!frozen) { data.lastSeen = lastActive; save(); } });

// ===== 7. DASHBOARD ==========================================================
function resumeTarget() {
  if (pending && byId(pending.sid)) return byId(pending.sid);
  return cur() || data.sessions.filter(function (s) { return s.items.length || s.stopped; })[0] || null;
}
function itemHtml(i) {
  var body = i.type === 'link' ? '<a href="' + esc(normUrl(i.text)) + '" target="_blank" rel="noopener">' + esc(i.text) + '</a>' :
             i.type === 'task' ? '<label><input type="checkbox" data-done="' + i.id + '" ' + (i.done ? 'checked' : '') + ' style="width:auto;margin:0 6px 0 0">' + esc(i.text) + '</label>' : esc(i.text);
  return '<div class="item ' + (i.done ? 'done' : '') + '"><span class="tag">' + i.type + '</span><span class="t">' + body + '</span><button class="x" data-del="' + i.id + '" title="Delete">&#10005;</button></div>';
}
function render() {
  if (!unlocked) return;
  var s = cur(), t = resumeTarget(), h = '';
  h += '<div class="topbar"><div><h1>ContextMind</h1><div class="muted">Workflow context recovery</div></div><button class="btn light small" id="lockBtn">Lock</button></div>';

  // Welcome back / resume card
  if (t) {
    var sum = smartSummary(t), links = t.items.filter(function (i) { return i.type === 'link'; }), open = t.items.filter(function (i) { return i.type === 'task' && !i.done; });
    h += '<h2>' + (pending ? 'Welcome back' : 'Resume your work') + '</h2><div class="card">';
    if (pending) h += '<div>You were away <b>' + pending.minutes + ' min</b> (' + esc(pending.type) + ').</div><br>';
    h += '<h3>' + esc(t.title) + '</h3><div class="muted">' + fmt(t.start) + (t.end ? ' to ' + fmt(t.end) : ' (running)') + '</div>';
    h += '<p><b>You were working on:</b> ' + esc(sum) + '</p>';
    h += '<p><b>Where you stopped:</b> ' + (t.stopped ? esc(t.stopped) : '<span class="muted">(not written)</span>') + '</p>';
    if (open.length) h += '<p><b>Pending tasks:</b> ' + open.map(function (o) { return esc(o.text); }).join(' | ') + '</p>';
    if (links.length) h += '<p><b>Links:</b><br>' + links.map(function (l) { return '<a href="' + esc(normUrl(l.text)) + '" target="_blank" rel="noopener">' + esc(l.text) + '</a>'; }).join('<br>') + '</p>';
    h += '<button class="btn big" data-resume="' + t.id + '">Resume work</button><div class="muted">Opens your saved links and brings this session back as the active one.</div></div>';
  }

  // Current session
  h += '<h2>Current session</h2>';
  if (s) {
    h += '<div class="card white" id="current"><h3>' + esc(s.title) + '</h3><div class="muted">Started ' + fmt(s.start) + ' | <span id="elapsed"></span> | idle <span id="idle">0</span>s</div>';
    h += '<div class="addrow"><select id="itype"><option value="note">Note</option><option value="link">Link</option><option value="task">Task</option></select><input id="itext" placeholder="Type and tap Add"><button class="btn" id="addBtn">Add</button></div>';
    h += '<div>' + (s.items.length ? s.items.map(itemHtml).join('') : '<p class="muted">Add notes, links and tasks while you work.</p>') + '</div>';
    h += '<p><b>Where I stopped / next step</b></p><textarea id="stopped" placeholder="Example: finished slide 5, next fix the results table">' + esc(s.stopped) + '</textarea>';
    h += '<button class="btn red small" id="endBtn">End session</button></div>';
  } else {
    h += '<div class="card white"><p class="muted">No session running.</p></div>';
  }
  h += '<div class="card"><div class="row"><input id="ntitle" placeholder="New session name (example: Review 2 project)"><button class="btn" id="newBtn">Start new session</button></div></div>';

  // Similar sessions (TF-IDF + cosine)
  var sim = t ? similarTo(t.id) : [];
  if (sim.length) {
    h += '<h2>Similar past sessions</h2><div class="card white list">' + sim.map(function (r) {
      return '<div class="li"><div style="flex:1">' + esc(r.s.title) + '<div class="score"><i style="width:' + Math.round(r.score * 100) + '%"></i></div></div><b>' + r.score.toFixed(2) + '</b><button class="btn small light" data-resume="' + r.s.id + '">Resume</button></div>';
    }).join('') + '</div><div class="muted">TF-IDF + Cosine Similarity, same method as the laptop version.</div>';
  }

  // Sessions list
  h += '<h2>All sessions</h2><div class="card white list">' + (data.sessions.length ? data.sessions.map(function (x) {
    return '<div class="li"><div style="flex:1"><b>' + esc(x.title) + '</b><div class="muted">' + fmt(x.start) + (x.end ? ' to ' + fmt(x.end) : ' (running)') + ' | ' + x.items.length + ' items</div></div>' +
      '<button class="btn small" data-resume="' + x.id + '">Resume</button><button class="btn small light" data-delsess="' + x.id + '">Delete</button></div>';
  }).join('') : '<p class="muted">No sessions yet. Start one above.</p>') + '</div>';

  // Break history
  h += '<h2>Break history</h2><div class="card white list">' + (data.breaks.length ? data.breaks.slice(0, 15).map(function (b) {
    var c = b.type.indexOf('Short') === 0 ? 'Short' : b.type.indexOf('Real') === 0 ? 'Real' : 'End';
    return '<div class="li"><div>' + fmt(b.ts) + '<div class="muted">' + esc(b.title || '') + '</div></div><div><span class="pill ' + c + '">' + esc(b.type) + '</span> ' + b.minutes + ' min</div></div>';
  }).join('') : '<p class="muted">No breaks yet.</p>') + '</div>';

  // Demo
  h += '<h2>Demo</h2><div class="card"><b>Simulate shutdown + restart</b><div class="muted">Saves your work, locks the page, and shows the welcome-back screen after you unlock, like switching the phone/laptop off and on.</div><button class="btn" id="demoBtn">Demo: simulate shutdown + restart</button></div>';

  // Settings
  var st = data.settings;
  h += '<h2>Settings</h2><div class="card"><div class="row"><label>Break after (sec idle)<input type="number" id="s1" min="10" value="' + st.breakSec + '"></label><label>Short interruption under (min)<input type="number" id="s2" min="1" value="' + st.shortMin + '"></label><label>End of session at (min)<input type="number" id="s3" min="2" value="' + st.endMin + '"></label><label>Auto-lock after (min)<input type="number" id="s4" min="1" value="' + st.autoLockMin + '"></label></div>' +
    '<button class="btn" id="saveSet">Save settings</button><hr><label>New password<input type="password" id="newpw" placeholder="Leave empty to keep current"></label><button class="btn light" id="savePw">Change password</button><hr>' +
    '<button class="btn light" id="exportBtn">Export backup</button> <label class="btn light" style="display:inline-block">Import backup<input type="file" id="importFile" accept=".json" style="display:none"></label> <button class="btn red small" id="wipeBtn">Delete all data</button>' +
    '<div class="muted">Your data is stored only in this browser on this device. Use Export / Import to move it between phone and laptop.</div></div>';

  $('app').innerHTML = h;
  wire();
}

function wire() {
  var q = function (sel) { return document.querySelectorAll(sel); };
  $('lockBtn').onclick = showLock;
  var add = $('addBtn'); if (add) { add.onclick = function () { addItem($('itype').value, $('itext').value); }; $('itext').addEventListener('keydown', function (e) { if (e.key === 'Enter') add.click(); }); }
  var st = $('stopped'); if (st) st.addEventListener('input', function () { var s = cur(); if (s) { s.stopped = st.value; save(); } });
  var en = $('endBtn'); if (en) en.onclick = function () { endCurrent(); save(); render(); };
  $('newBtn').onclick = function () { newSession($('ntitle').value); };
  Array.prototype.forEach.call(q('[data-resume]'), function (b) { b.onclick = function () { resumeSession(b.getAttribute('data-resume')); }; });
  Array.prototype.forEach.call(q('[data-del]'), function (b) { b.onclick = function () { var s = cur(); s.items = s.items.filter(function (i) { return i.id !== b.getAttribute('data-del'); }); save(); render(); }; });
  Array.prototype.forEach.call(q('[data-done]'), function (c) { c.onchange = function () { var s = cur(); s.items.forEach(function (i) { if (i.id === c.getAttribute('data-done')) i.done = c.checked; }); save(); render(); }; });
  Array.prototype.forEach.call(q('[data-delsess]'), function (b) { b.onclick = function () { if (!confirm('Delete this session?')) return; var id = b.getAttribute('data-delsess'); data.sessions = data.sessions.filter(function (s) { return s.id !== id; }); if (data.currentId === id) data.currentId = null; save(); render(); }; });
  $('saveSet').onclick = function () {
    var v = [+$('s1').value, +$('s2').value, +$('s3').value, +$('s4').value];
    if (v.some(function (x) { return !(x > 0); })) { toast('Enter numbers above 0'); return; }
    data.settings = { breakSec: v[0], shortMin: v[1], endMin: v[2], autoLockMin: v[3] }; save(); toast('Settings saved');
  };
  $('savePw').onclick = function () { var p = $('newpw').value; if (p.length < 4) { toast('Use at least 4 characters'); return; } setPw(p).then(function () { toast('Password changed'); $('newpw').value = ''; }); };
  $('exportBtn').onclick = function () {
    var a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
    a.download = 'contextmind-backup.json'; a.click();
  };
  $('importFile').onchange = function (e) {
    var f = e.target.files[0]; if (!f) return; var r = new FileReader();
    r.onload = function () { try { var d = JSON.parse(r.result); if (!d.sessions) throw 0; data = Object.assign(clone(DEFAULTS), d); save(); render(); toast('Backup imported'); } catch (x) { toast('Not a valid backup file'); } };
    r.readAsText(f);
  };
  $('wipeBtn').onclick = function () { if (confirm('Delete ALL sessions and settings?')) { data = clone(DEFAULTS); pending = null; save(); render(); } };
  $('demoBtn').onclick = function () {
    frozen = true;                                      // stop normal saving so the fake "away time" is kept
    data.lastS})();
