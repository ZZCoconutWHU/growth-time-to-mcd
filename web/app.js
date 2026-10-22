/* ============================================================
   麦麦成长时刻 · 前端渲染 + 交互
   - 数据：优先 /api/*（python run.py web 起的服务），否则离线 stats.js / words.js
   - 公式：与后端 src/time_to_mcd/coach.py 完全一致（BMI/BMR/TDEE/分餐）
   ============================================================ */
(function () {
  "use strict";

  var $ = function (s) { return document.querySelector(s); };
  var $$ = function (s) { return Array.prototype.slice.call(document.querySelectorAll(s)); };

  function fmt(n, d) {
    var v = Number(n) || 0; d = d == null ? 0 : d;
    return v.toLocaleString("zh-CN", { minimumFractionDigits: d, maximumFractionDigits: d });
  }
  function icon(id, cls) {
    return '<svg class="ico ' + (cls || "") + '"><use href="#i-' + id + '"/></svg>';
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  /* ---------------- 与后端一致的公式（单一真源的前端镜像） ---------------- */
  var ACTIVITY = { sedentary: 1.2, light: 1.375, moderate: 1.55, active: 1.725 };
  var GOAL_ADJ = { lose: -0.15, maintain: 0.0, gain: 0.10 };
  var PROTEIN_PER_KG = { lose: 1.8, maintain: 1.5, gain: 1.8 };
  var MEAL_SHARE = { breakfast: 0.25, lunch: 0.35, dinner: 0.30, snack: 0.10 };
  var MEAL_LABEL = { breakfast: "早餐", lunch: "午餐", dinner: "晚餐", snack: "加餐" };
  var GOAL_LABEL = { lose: "减脂", maintain: "维持", gain: "增肌" };

  function calcBMI(h, w) { return w / Math.pow(h / 100, 2); }
  function bmiBand(v) {
    if (v < 18.5) return "偏瘦";
    if (v < 24) return "正常";
    if (v < 28) return "超重";
    return "肥胖";
  }
  function bmr(h, w, age, sex) { return 10 * w + 6.25 * h - 5 * age + (sex === "male" ? 5 : -161); }
  function tdee(b, act) { return b * (ACTIVITY[act] || 1.375); }

  function mealPlan(p) {
    var b = bmr(p.height_cm, p.weight_kg, p.age, p.sex);
    var t = tdee(b, p.activity);
    var dailyKcal = t * (1 + (GOAL_ADJ[p.goal] || 0));
    var dailyProtein = p.weight_kg * (PROTEIN_PER_KG[p.goal] || 1.5);
    var meals = {};
    Object.keys(MEAL_SHARE).forEach(function (m) {
      var share = MEAL_SHARE[m];
      var kcal = dailyKcal * share;
      var protein = Math.max(15, dailyProtein * share);
      var fat = kcal * 0.30 / 9;
      var carb = Math.max(0, (kcal - protein * 4 - fat * 9) / 4);
      meals[m] = { label: MEAL_LABEL[m], share: share, kcal: Math.round(kcal), protein: Math.round(protein * 10) / 10, fat: Math.round(fat * 10) / 10, carb: Math.round(carb * 10) / 10 };
    });
    return {
      bmi: Math.round(calcBMI(p.height_cm, p.weight_kg) * 10) / 10,
      bmiBand: bmiBand(calcBMI(p.height_cm, p.weight_kg)),
      bmr: Math.round(b), tdee: Math.round(t),
      dailyKcal: Math.round(dailyKcal), dailyProtein: Math.round(dailyProtein * 10) / 10,
      meals: meals, goalLabel: GOAL_LABEL[p.goal] || p.goal
    };
  }

  /* ---------------- 数据获取（在线优先，离线兜底） ---------------- */
  var DATA = null, WORDS = [];

  function fetchJSON(path) {
    return fetch(path, { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }
  function api(method, path, body) {
    if (!window.fetch) return Promise.resolve(null);
    var opt = { method: method, headers: { "Content-Type": "application/json" } };
    if (body) opt.body = JSON.stringify(body);
    return fetch(path, opt).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    }).catch(function () { return null; });
  }

  function loadData(cb) {
    fetchJSON("/api/stats").then(function (d) { DATA = d; cb(); })
      .catch(function () { DATA = window.T2M_DATA || null; cb(); });
    // 词库顺手缓存到手机：出了门没网也能继续背
    fetchJSON("/api/words").then(function (w) { if (w && w.length) { WORDS = w; cacheWords(w); } })
      .catch(function () { WORDS = window.T2M_WORDS || readWordCache() || []; });
    // 兜底：即便 /api 都失败，也用离线数据先渲染
    if (!DATA) DATA = window.T2M_DATA || null;
    if (!WORDS.length) WORDS = window.T2M_WORDS || [];
  }

  /* ---------------- 手机 ↔ 电脑同步（离线排队 → 回来补传） ---------------- */
  /* 手机是要带出门的：一离开家里的 WiFi 就访问不到电脑上跑的服务。
     这时不能让「背的单词白背」——写操作先排进浏览器的待发队列，页面照常用；
     回到同一个 WiFi（或任何能访问到电脑的网络）后自动补传。
     每条记录自带 uid，重复上传只会落一次，不会把卡路里或单词数算两遍。 */
  var OUTBOX_KEY = "t2m_outbox_v1", DEVICE_KEY = "t2m_device_id", WORD_CACHE_KEY = "t2m_words_v1";
  var sync = { online: null, lastSyncAt: "", lanUrl: "", serverStats: null, flushing: false, pending: 0 };

  function deviceId() {
    var id = "";
    try { id = localStorage.getItem(DEVICE_KEY) || ""; } catch (e) {}
    if (!id) {
      id = "d" + Math.random().toString(36).slice(2, 8);
      try { localStorage.setItem(DEVICE_KEY, id); } catch (e) {}
    }
    return id;
  }
  function newUid() {
    return deviceId() + "-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 6);
  }
  function loadOutbox() {
    try { return JSON.parse(localStorage.getItem(OUTBOX_KEY) || "[]") || []; } catch (e) { return []; }
  }
  function saveOutbox(list) {
    try { localStorage.setItem(OUTBOX_KEY, JSON.stringify(list || [])); } catch (e) {}
    sync.pending = (list || []).length;
  }
  function enqueue(op) {
    var list = loadOutbox();
    list.push(op);
    saveOutbox(list);
  }
  function cacheWords(list) {
    try { localStorage.setItem(WORD_CACHE_KEY, JSON.stringify(list || [])); } catch (e) {}
  }
  function readWordCache() {
    try { return JSON.parse(localStorage.getItem(WORD_CACHE_KEY) || "[]") || []; } catch (e) { return []; }
  }

  /* 带 uid/设备号的写请求：服务端认 uid，重传只落一次 */
  function postOp(op) {
    var body = {};
    for (var k in (op.payload || {})) body[k] = op.payload[k];
    body.uid = op.uid; body.ts = op.ts; body.device = deviceId();
    return fetch(op.path, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
    }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  /* 所有写操作都走这里：能发就发，发不出去就排队 */
  function submit(type, path, payload) {
    var op = { type: type, path: path, uid: newUid(), ts: new Date().toISOString(), payload: payload };
    if (!window.fetch) return Promise.resolve(null);
    return postOp(op).then(function (r) {
      if (r && !r.error) { markOnline(true); renderSync(); return r; }
      enqueue(op); markOnline(false); renderSync();
      return null;
    }).catch(function () {
      enqueue(op); markOnline(false); renderSync();
      return null;
    });
  }

  /* 回到 WiFi 后把攒下的记录逐条补传；失败的留在队列里等下次 */
  function flushOutbox() {
    var list = loadOutbox();
    if (!list.length || sync.flushing) return Promise.resolve(0);
    sync.flushing = true;
    var rest = [], done = 0;
    function next(i) {
      if (i >= list.length) {
        saveOutbox(rest);
        sync.flushing = false;
        renderSync();
        if (done) refreshStats();
        return Promise.resolve(done);
      }
      return postOp(list[i]).then(function (r) {
        if (r && !r.error) done++; else rest.push(list[i]);
      }).catch(function () {
        rest.push(list[i]);
      }).then(function () { return next(i + 1); });
    }
    return next(0);
  }

  function markOnline(flag) {
    sync.online = !!flag;
  }

  /* 探测电脑上的服务在不在（手机回家后靠它触发自动补传） */
  function probeSync() {
    if (!window.fetch) return Promise.resolve(false);
    return fetchJSON("/api/sync/status").then(function (d) {
      if (!d || !d.ok) throw new Error("no service");
      markOnline(true);
      sync.lastSyncAt = d.last_sync_at || "";
      sync.lanUrl = d.lan_url || "";
      sync.serverStats = d.stats || null;
      renderSync();
      return flushOutbox();
    }).catch(function () {
      markOnline(false);
      renderSync();
      return false;
    });
  }

  function renderSync() {
    var n = loadOutbox().length;
    sync.pending = n;
    var chip = $("#syncChip"), txt = $("#syncChipText");
    if (txt) txt.textContent = n ? ("待同步 " + n) : (sync.online === false ? "离线" : "已同步");
    if (chip) {
      chip.classList.toggle("is-pending", n > 0);
      chip.classList.toggle("is-offline", sync.online === false);
    }
    var state = $("#syncState"), sum = $("#syncSummary"), hint = $("#syncHint"), lan = $("#syncLan");
    if (state) state.textContent = sync.online === false ? "离线 · 连不上电脑上的服务" : (sync.online ? "已连上电脑" : "检测中…");
    if (sum) {
      var s = sync.serverStats;
      sum.innerHTML = s
        ? ("电脑档案：<b>" + fmt(s.meals_total) + "</b> 餐 · <b>" + fmt(s.words_studied) + "</b> 词 · 专注 <b>" + fmt(s.focus_minutes) + "</b> 分钟" +
           (sync.lastSyncAt ? " · 上次同步 " + esc(String(sync.lastSyncAt).slice(5, 16).replace("T", " ")) : ""))
        : "还没读到电脑上的档案（离线，或电脑上的服务没开）。";
    }
    if (hint) {
      hint.innerHTML = n
        ? "手机里攒了 <b>" + n + "</b> 条记录（背词/一餐/通勤）。回到同一个 WiFi 打开这个页面，会自动补传到电脑；每条带 uid，重传不会重复计算。"
        : "手机和电脑在同一个 WiFi 下打开的就是同一份档案，写进去即同步，不用管它。";
    }
    if (lan && sync.lanUrl) {
      lan.innerHTML = '手机连同一个 WiFi 后访问 <code>' + esc(sync.lanUrl) + '</code>（电脑用 <code>python run.py web --host 0.0.0.0</code> 启动）。';
    }
  }

  function downloadJSON(name, data) {
    var blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  }

  function exportBackup() {
    var d = new Date();
    var pad = function (x) { return (x < 10 ? "0" : "") + x; };
    var name = "mcmcp-" + d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate()) + "-" + pad(d.getHours()) + pad(d.getMinutes()) + ".json";
    fetchJSON("/api/sync/pull").then(function (snap) {
      downloadJSON(name, snap);
      $("#syncState").textContent = "已导出电脑上的完整档案";
    }).catch(function () {
      // 离线也能导出：至少把手机里攒的记录带出去
      downloadJSON(name, {
        generated_at: new Date().toISOString(),
        note: "离线导出：只有手机待同步的记录",
        pending: loadOutbox()
      });
      $("#syncState").textContent = "离线导出：只带了手机待同步的记录";
    });
  }

  function importBackup(file) {
    var reader = new FileReader();
    reader.onload = function () {
      var data = null;
      try { data = JSON.parse(reader.result); } catch (e) {
        $("#syncState").textContent = "这个文件不是合法的 JSON";
        return;
      }
      var ops = data.pending || data.ops || [];
      var queued = 0;
      ops.forEach(function (op) {
        if (op && op.uid && op.path && op.payload) { enqueue(op); queued++; }
      });
      var isSnapshot = !!(data.meals || data.sessions || data.word_progress);
      var after = function (r) {
        $("#syncState").textContent = isSnapshot
          ? ((r && r.ok) ? ("已导入：餐 " + ((r.merged || {}).meals || 0) + " · 会话 " + ((r.merged || {}).sessions || 0) + " · 词 " + ((r.merged || {}).word_progress || 0)) : "导入失败：电脑上的服务没连上")
          : ("已把 " + queued + " 条记录放进待同步队列");
        renderSync();
        refreshStats();
      };
      if (isSnapshot) {
        api("POST", "/api/sync/push", { device: deviceId(), snapshot: data }).then(after);
      } else {
        after(null);
        probeSync();
      }
    };
    reader.readAsText(file);
  }

  function syncInit() {
    renderSync();
    probeSync();
    var chip = $("#syncChip");
    if (chip) chip.addEventListener("click", function () {
      switchScreen("meal");
      probeSync();
    });
    var now = $("#syncNow");
    if (now) now.addEventListener("click", function () {
      now.disabled = true;
      probeSync().then(function () { now.disabled = false; });
    });
    var exp = $("#syncExport");
    if (exp) exp.addEventListener("click", exportBackup);
    var imp = $("#syncImportFile");
    if (imp) imp.addEventListener("change", function () {
      if (imp.files && imp.files[0]) importBackup(imp.files[0]);
      imp.value = "";
    });
    // 网络状态变化 / 回到前台 / 每 30 秒，都再探一次，做到「回家即同步」
    window.addEventListener("online", function () { probeSync(); });
    window.addEventListener("offline", function () { markOnline(false); renderSync(); });
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) probeSync();
    });
    setInterval(probeSync, 30000);
  }

  /* ---------------- 数字滚动 ---------------- */
  /* ---------------- 吉祥物：随等级换装备 ---------------- */
  /* 像素汉堡会跟着称号升级：从「光秃秃一小只」一路到「金拱门主厨 + 皇冠」。
     每级只在前一级基础上加件，造型递进才看得出成长。 */
  var MASCOT_GEAR = [
    // 0 见习麦友
    function () { return ""; },
    // 1 麦苗新芽：头顶冒两片小芽
    function () {
      return '<g class="gear-sprout">' +
        '<rect x="14" y="-2" width="2" height="4" fill="#6DBE45"/>' +
        '<rect x="12" y="-4" width="3" height="2" fill="#8FD35F"/><rect x="16" y="-5" width="3" height="2" fill="#8FD35F"/>' +
        '<rect x="11" y="-3" width="1" height="1" fill="#6DBE45"/><rect x="19" y="-4" width="1" height="1" fill="#6DBE45"/>' +
        "</g>";
    },
    // 2 麦香学徒：+ 红白格小方巾
    function () {
      return MASCOT_GEAR[1]() +
        '<g class="gear-scarf">' +
        '<rect x="8" y="14" width="16" height="3" fill="#DA291C"/>' +
        '<rect x="10" y="14" width="2" height="3" fill="#FFF6E0"/><rect x="15" y="14" width="2" height="3" fill="#FFF6E0"/>' +
        '<rect x="8" y="17" width="3" height="2" fill="#DA291C"/>' +
        "</g>";
    },
    // 3 汉堡侍从：+ 端着托盘
    function () {
      return MASCOT_GEAR[2]() +
        '<g class="gear-tray">' +
        '<rect x="-8" y="15" width="9" height="2" fill="#C9CDD2"/><rect x="-7" y="17" width="7" height="1" fill="#9AA1A9"/>' +
        '<rect x="-6" y="13" width="3" height="2" fill="#FFC72C"/>' +
        "</g>";
    },
    // 4 薯条骑士：+ 手持薯条 + 小披风
    function () {
      return MASCOT_GEAR[3]() +
        '<g class="gear-cape"><rect x="-6" y="11" width="4" height="13" fill="#DA291C"/>' +
        '<rect x="32" y="11" width="4" height="13" fill="#DA291C"/>' +
        '<rect x="-6" y="24" width="4" height="1" fill="#A81D14"/><rect x="32" y="24" width="4" height="1" fill="#A81D14"/></g>' +
        '<g class="gear-fries">' +
        '<rect x="30" y="12" width="2" height="7" fill="#FFD24A"/><rect x="33" y="10" width="2" height="9" fill="#FFD24A"/>' +
        '<rect x="36" y="13" width="2" height="6" fill="#FFD24A"/>' +
        '<rect x="29" y="17" width="10" height="7" fill="#DA291C"/><rect x="29" y="17" width="10" height="1" fill="#FF6B5B"/>' +
        '<rect x="31" y="19" width="6" height="4" fill="#FFC72C"/>' +
        "</g>";
    },
    // 5 麦麦达人：+ 墨镜
    function () {
      return MASCOT_GEAR[4]() +
        '<g class="gear-shades">' +
        '<rect x="7" y="16" width="18" height="3" fill="#2B1A0E"/>' +
        '<rect x="9" y="16" width="4" height="2" fill="#4A5A6B"/><rect x="19" y="16" width="4" height="2" fill="#4A5A6B"/>' +
        '<rect x="15" y="17" width="2" height="1" fill="#2B1A0E"/>' +
        "</g>";
    },
    // 6 金拱门主厨：+ 厨师帽
    function () {
      return MASCOT_GEAR[5]() +
        '<g class="gear-hat">' +
        '<rect x="9" y="-1" width="14" height="4" fill="#FFF6E0"/><rect x="8" y="3" width="16" height="2" fill="#EDE3CE"/>' +
        '<rect x="8" y="-5" width="4" height="4" fill="#FFF6E0"/><rect x="13" y="-7" width="6" height="6" fill="#FFF6E0"/>' +
        '<rect x="20" y="-5" width="4" height="4" fill="#FFF6E0"/>' +
        '<rect x="14" y="-8" width="4" height="1" fill="#FFFDF5"/>' +
        "</g>";
    },
    // 7 麦麦传奇：+ 皇冠 + 闪光
    function () {
      return MASCOT_GEAR[6]() +
        '<g class="gear-crown">' +
        '<rect x="12" y="-13" width="8" height="3" fill="#FFC72C"/>' +
        '<rect x="12" y="-15" width="2" height="2" fill="#FFC72C"/><rect x="17" y="-15" width="2" height="2" fill="#FFC72C"/>' +
        '<rect x="15" y="-16" width="2" height="2" fill="#FFD24A"/>' +
        '<rect x="13" y="-11" width="1" height="1" fill="#DA291C"/><rect x="18" y="-11" width="1" height="1" fill="#6DBE45"/>' +
        "</g>" +
        '<g class="gear-sparkle">' +
        '<rect x="2" y="-6" width="2" height="2" fill="#FFC72C"/><rect x="27" y="-9" width="2" height="2" fill="#FFD24A"/>' +
        '<rect x="-2" y="4" width="2" height="2" fill="#FF6B5B"/>' +
        "</g>";
    }
  ];

  function renderMascot(lv) {
    var box = $("#mascotGear");
    if (!box) return;
    var idx = Math.max(0, Math.min(MASCOT_GEAR.length - 1, Number(lv && lv.index) || 0));
    box.innerHTML = MASCOT_GEAR[idx]();
    var art = document.querySelector(".mascot");
    if (art) art.setAttribute("aria-label", "像素汉堡吉祥物 · " + (lv && lv.title ? lv.title : ""));
  }

  function countUp(el, target, decimals) {
    if (!el) return;
    var dur = 700, steps = 18, i = 0;
    var timer = setInterval(function () {
      i++;
      el.textContent = fmt(target * (i / steps), decimals);
      if (i >= steps) { clearInterval(timer); el.textContent = fmt(target, decimals); }
    }, dur / steps);
  }

  /* =============================== 看板屏 =============================== */
  function renderTop(d) {
    var s = d.stats, lv = d.level;
    var map = {
      streak: s.streak_days, active_days: s.active_days, level_title: lv.title, level_title2: lv.title,
      level_index: lv.index, xp: lv.xp, next_need: lv.next_need, level_motto: lv.motto,
      focus_minutes: fmt(s.focus_minutes, 1), trips: s.trips, sessions: s.sessions,
      // 专注时间 = 数据库里 sessions 的真实时长，说清楚是背单词还是等餐
      focus_note: s.focus_minutes
        ? "专注分钟来自本地库：背单词 " + fmt(s.focus_words_minutes || 0, 1) +
          " 分钟 · 等餐 " + fmt(s.focus_wait_minutes || 0, 1) + " 分钟"
        : "",
      kcal_in: fmt(s.kcal_in), sodium_in: fmt(s.sodium_in),
      avg_meal_kcal: fmt(s.meals_total ? s.kcal_in / s.meals_total : 0),
      db: d.db, generated_at: (d.generated_at || "").replace("T", " ")
    };
    Object.keys(map).forEach(function (k) {
      $$('[data-bind="' + k + '"]').forEach(function (el) { el.textContent = map[k]; });
    });
    var fill = $('[data-bar="level_percent"]');
    if (fill) setTimeout(function () { fill.style.width = lv.percent + "%"; }, 120);
    renderMascot(lv);
  }

  function renderCards(d) {
    var s = d.stats, dv = d.derived, byCode = {};
    d.badges.forEach(function (b) { byCode[b.code] = b; });
    var cards = [
      { cls: "c-green", icon: "burger", title: "健康饮食", value: s.meals_on_target, unit: "餐",
        sub: "共 " + s.meals_total + " 餐记录 · 达标率 " + dv.on_target_rate + "%",
        pct: byCode.meals_10 ? byCode.meals_10.percent : 0,
        cap: "下一档「十餐知己」还差 " + Math.max(0, 10 - s.meals_on_target) + " 餐",
        foot: "落在热量与蛋白目标区间内才算达标" },
      { cls: "c-blue", icon: "book", title: "学会的单词", value: s.words_studied, unit: "个",
        sub: "已掌握 " + s.words_mastered + " 个 · 正确率 " + s.word_accuracy + "%",
        pct: byCode.words_100 ? byCode.words_100.percent : 0,
        cap: "下一档「百词斩」还差 " + Math.max(0, 100 - s.words_studied) + " 个",
        foot: dv.words_per_min ? "等餐 1 分钟大约能刷 " + dv.words_per_min + " 个词" : "还没有背词记录，等餐时来一局" },
      { cls: "c-red", icon: "flame", title: "消耗的热量", value: s.kcal_burned, unit: "kcal", decimals: 1,
        sub: "步行 / 骑行 " + s.distance_km + " km · " + s.commute_minutes + " 分钟 · " + s.trips + " 趟",
        pct: byCode.burn_500 ? byCode.burn_500.percent : 0,
        cap: "下一档「燃烧五百」还差 " + fmt(Math.max(0, 500 - s.kcal_burned), 1) + " kcal",
        foot: "≈ " + dv.burger_equivalent + " 个巨无霸（按 550 kcal 折算）" },
      { cls: "c-purple", icon: "meat", title: "摄入的蛋白质", value: s.protein_in, unit: "g", decimals: 1,
        sub: "≈ " + dv.egg_equivalent + " 个鸡蛋 · 平均每餐 " + fmt(s.meals_total ? s.protein_in / s.meals_total : 0, 1) + " g",
        pct: byCode.protein_1kg ? byCode.protein_1kg.percent : 0,
        cap: "下一档「蛋白一公斤」还差 " + fmt(Math.max(0, 1000 - s.protein_in), 1) + " g",
        foot: "碳水 " + fmt(s.carb_in) + " g · 脂肪 " + fmt(s.fat_in) + " g" }
    ];
    $("#cards").innerHTML = cards.map(function (c) {
      return '<div class="card ' + c.cls + '"><div class="card-top">' + icon(c.icon) + "<span>" + c.title + "</span></div>" +
        '<div class="card-body"><div class="card-num"><span data-num>' + fmt(c.value, c.decimals || 0) + "</span><span class=\"unit\">" + c.unit + "</span></div>" +
        '<p class="card-sub">' + c.sub + "</p>" +
        '<div class="bar"><i class="bar-fill" data-bar style="width:0%"></i></div>' +
        '<p class="bar-cap">' + c.cap + "</p>" +
        '<div class="card-foot">' + icon("star") + "<span>" + c.foot + "</span></div></div></div>";
    }).join("");
    $$("#cards .card").forEach(function (card, i) {
      var c = cards[i];
      setTimeout(function () { countUp(card.querySelector("[data-num]"), c.value, c.decimals || 0); }, 80 * i);
      setTimeout(function () { card.querySelector("[data-bar]").style.width = Math.min(100, c.pct) + "%"; }, 200 + 80 * i);
    });
  }

  var METRICS = {
    words: { label: "背单词", unit: "个", cls: "is-blue", color: "var(--blue)" },
    burn_kcal: { label: "消耗热量", unit: "kcal", cls: "", color: "var(--yellow)" },
    on_target: { label: "达标餐", unit: "餐", cls: "is-green", color: "var(--green-l)" }
  };
  function renderChart(d, metric) {
    var cfg = METRICS[metric] || METRICS.words, days = d.daily || [];
    var max = Math.max.apply(null, days.map(function (x) { return Number(x[metric]) || 0; }).concat([1]));
    var total = days.reduce(function (a, x) { return a + (Number(x[metric]) || 0); }, 0);
    $("#chart").className = "chart " + cfg.cls;
    $("#chart").innerHTML = days.map(function (x) {
      var v = Number(x[metric]) || 0;
      var h = Math.max(v > 0 ? 8 : 0, Math.round(v / max * 100));
      return '<div class="chart-col"><div class="chart-bar"><i data-h="' + h + '" style="height:0%"></i></div>' +
        '<div class="chart-val">' + fmt(v, metric === "burn_kcal" ? 1 : 0) + "</div>" +
        '<div class="chart-day">' + x.date.slice(5) + "</div></div>";
    }).join("");
    setTimeout(function () {
      $$("#chart .chart-bar i").forEach(function (el, i) {
        setTimeout(function () { el.style.height = el.getAttribute("data-h") + "%"; }, 40 * i);
      });
    }, 60);
    $("#chartNote").textContent = "近 7 天共 " + cfg.label + " " + fmt(total, metric === "burn_kcal" ? 1 : 0) + " " + cfg.unit + "，柱子高度按最大值等比缩放。";
  }
  function bindTabs(d) {
    $$("#chartTabs .tab").forEach(function (btn) {
      btn.addEventListener("click", function () {
        $$("#chartTabs .tab").forEach(function (b) { b.classList.remove("is-on"); });
        btn.classList.add("is-on"); renderChart(d, btn.getAttribute("data-metric"));
      });
    });
  }
  function renderMacros(d) {
    var s = d.stats;
    var parts = [
      { name: "碳水", g: s.carb_in, kcal: s.carb_in * 4, color: "#ff8a1f" },
      { name: "蛋白质", g: s.protein_in, kcal: s.protein_in * 4, color: "#3fa34d" },
      { name: "脂肪", g: s.fat_in, kcal: s.fat_in * 9, color: "#8e5bd0" }
    ];
    var sum = parts.reduce(function (a, p) { return a + p.kcal; }, 0) || 1;
    $("#macros").innerHTML = parts.map(function (p) {
      var pct = Math.round(p.kcal / sum * 100);
      return '<div class="macro-row"><span class="m-name">' + p.name + "</span>" +
        '<div class="macro-bar"><i data-w="' + pct + '" style="width:0%;background-color:' + p.color + '"></i></div>' +
        '<span class="m-val">' + fmt(p.g) + "g " + pct + "%</span></div>";
    }).join("");
    setTimeout(function () { $$("#macros .macro-bar i").forEach(function (el) { el.style.width = el.getAttribute("data-w") + "%"; }); }, 200);
  }
  function renderBadges(d) {
    var on = d.badges.filter(function (b) { return b.unlocked; }).length;
    $("#badgeCount").textContent = on + " / " + d.badges.length;
    $("#badges").innerHTML = d.badges.map(function (b) {
      return '<div class="badge ' + (b.unlocked ? "on" : "off") + '"><div class="badge-ico">' + icon(b.unlocked ? b.icon : "lock") + "</div><div>" +
        '<div class="badge-name">' + b.name + (b.unlocked ? "" : " 🔒") + "</div>" +
        '<div class="badge-desc">' + b.desc + "</div>" +
        '<div class="badge-prog">' + fmt(b.current, 1) + " / " + fmt(b.goal, 0) + "</div></div></div>";
    }).join("");
  }
  function renderTimeline(d) {
    var r = d.recent || [], rows = [];
    (r.meals || []).forEach(function (m) {
      var items = []; try { items = JSON.parse(m.items_json || "[]"); } catch (e) {}
      var names = items.map(function (i) { return i.name + "×" + i.qty; }).join(" + ");
      rows.push({ t: m.ts, ok: !!m.on_target,
        main: (m.store || "麦当劳") + " · " + ({ breakfast: "早餐", lunch: "午餐", dinner: "晚餐", snack: "加餐" }[m.meal_type] || m.meal_type),
        sub: names || "—", num: fmt(m.kcal) + " kcal · " + fmt(m.protein, 1) + "g 蛋白" });
    });
    (r.commutes || []).forEach(function (c) {
      rows.push({ t: c.ts, ok: true, main: (c.mode === "bike" ? "骑行" : c.mode === "drive" ? "驾车" : "步行") + "前往 " + (c.store || "门店"),
        sub: fmt(c.distance_m / 1000, 2) + " km · " + fmt(c.minutes, 1) + " 分钟", num: "消耗 " + fmt(c.kcal, 1) + " kcal" });
    });
    (r.sessions || []).forEach(function (x) {
      rows.push({ t: x.ts, ok: true, main: "等餐 / 路上背词 " + fmt(x.duration_min, 1) + " 分钟",
        sub: "答对 " + (x.correct || 0) + " / " + (x.words || 0) + " 个", num: "+" + (x.words || 0) + " 词" });
    });
    rows.sort(function (a, b) { return a.t < b.t ? 1 : -1; }); rows = rows.slice(0, 8);
    $("#timeline").innerHTML = rows.length
      ? rows.map(function (x) {
          return '<div class="tl-item ' + (x.ok ? "ok" : "no") + '"><span class="tl-time">' + x.t.replace("T", " ").slice(5, 16) +
            '</span><span class="tl-main"><b>' + esc(x.main) + "</b><span>" + esc(x.sub) + "</span></span><span class=\"tl-num\">" + esc(x.num) + "</span></div>";
        }).join("")
      : '<p class="note">还没有记录。到店后用本页「本餐热量」记录一餐即可。</p>';
  }

  /* =============================== 档案屏 =============================== */
  function readProfileForm() {
    return {
      height_cm: parseFloat($("#pf-height").value) || 0,
      weight_kg: parseFloat($("#pf-weight").value) || 0,
      age: parseInt($("#pf-age").value, 10) || 0,
      sex: $("#pf-sex").value, activity: $("#pf-activity").value, goal: $("#pf-goal").value
    };
  }
  function renderProfileResult(plan) {
    var bandColor = plan.bmiBand === "正常" ? "var(--green)" : (plan.bmiBand === "偏瘦" ? "var(--blue)" : "var(--red)");
    var html =
      '<div class="bmi-hero">' +
        '<div class="bmi-num" style="color:' + bandColor + '">' + fmt(plan.bmi, 1) + '</div>' +
        '<div class="bmi-meta"><span class="bmi-band" style="background:' + bandColor + '">' + plan.bmiBand + '</span>' +
        '<span>BMI（体重÷身高²）</span></div></div>' +
      '<div class="kpi-row">' +
        kpi("基础代谢 BMR", fmt(plan.bmr) + " kcal", "静息时的最低消耗") +
        kpi("每日总消耗 TDEE", fmt(plan.tdee) + " kcal", "含活动量的总支出") +
        kpi("每日热量预算", fmt(plan.dailyKcal) + " kcal", "目标：" + plan.goalLabel) +
        kpi("每日蛋白质", fmt(plan.dailyProtein, 1) + " g", "约 " + fmt(plan.dailyProtein / 6, 1) + " 个鸡蛋") +
      '</div>';
    $("#profileResult").innerHTML = html;

    // 三餐拆分
    var grid = "";
    Object.keys(MEAL_SHARE).forEach(function (m) {
      var v = plan.meals[m];
      grid += '<div class="meal-card"><div class="meal-card-h">' + icon("burger") + v.label +
        ' <span class="meal-share">' + Math.round(v.share * 100) + "%</span></div>" +
        mealStat("热量", v.kcal, "kcal") + mealStat("蛋白", v.protein, "g", "var(--green)") +
        mealStat("脂肪", v.fat, "g", "var(--purple)") + mealStat("碳水", v.carb, "g", "#ff8a1f") + "</div>";
    });
    $("#mealGrid").innerHTML = grid;
    $("#pf-daily").textContent = "每日 " + fmt(plan.dailyKcal) + " kcal";
    $("#pf-guideline").textContent = "参考《中国居民膳食指南（2022）》：食物多样、合理搭配；餐次能量 早25–30%/午30–40%/晚30–35%；蛋白供能10–15%、脂肪20–30%、碳水50–65%。";
  }
  function kpi(title, val, sub) {
    return '<div class="kpi"><div class="kpi-t">' + title + '</div><div class="kpi-v">' + val + '</div><div class="kpi-s">' + sub + "</div></div>";
  }
  function mealStat(name, val, unit, color) {
    return '<div class="meal-stat"><span class="ms-name">' + name + '</span><span class="ms-val" style="color:' + (color || "var(--ink)") + '">' + fmt(val, name === "热量" ? 0 : 1) + '<i>' + unit + "</i></span></div>";
  }

  function profileInit() {
    // 优先用服务端返回的档案（含 bmi/目标），否则用离线默认
    if (DATA && DATA.profile && DATA.profile.height_cm) {
      var p = DATA.profile;
      $("#pf-height").value = p.height_cm; $("#pf-weight").value = p.weight_kg;
      $("#pf-age").value = p.age; $("#pf-sex").value = p.sex || "male";
      $("#pf-activity").value = p.activity || "light"; $("#pf-goal").value = p.goal || "maintain";
      if (p.targets) {
        renderProfileResult({
          bmi: p.bmi, bmiBand: p.bmi_band, bmr: p.bmr || 0, tdee: p.tdee || 0,
          dailyKcal: 0, dailyProtein: 0, meals: p.targets, goalLabel: GOAL_LABEL[p.goal] || p.goal
        });
      }
    }
    $("#pf-calc").addEventListener("click", function () {
      var p = readProfileForm();
      if (!p.height_cm || !p.weight_kg || !p.age) { $("#pf-status").textContent = "请先填完身高/体重/年龄"; return; }
      renderProfileResult(mealPlan(p));
      $("#pf-status").textContent = "已按公式计算。点「保存档案」可写回数据库。";
    });
    $("#pf-save").addEventListener("click", function () {
      var p = readProfileForm();
      submit("profile", "/api/profile", p).then(function (resp) {
        if (resp && resp.plan) {
          renderProfileResult({
            bmi: resp.plan.summary.bmi, bmiBand: resp.plan.summary.bmi_band, bmr: resp.plan.summary.bmr,
            tdee: resp.plan.summary.tdee, dailyKcal: resp.plan.daily_kcal, dailyProtein: resp.plan.daily_protein,
            meals: resp.plan.meals, goalLabel: resp.plan.goal_label
          });
          $("#pf-status").textContent = "✓ 已保存到本地数据库（records.db）。";
          refreshStats();
        } else {
          // 离线兜底：存 localStorage
          try { localStorage.setItem("t2m_profile", JSON.stringify(p)); } catch (e) {}
          renderProfileResult(mealPlan(p));
          $("#pf-status").textContent = "（离线）已存到手机，回到同一个 WiFi 会自动补传到电脑。";
        }
      });
    });
  }

  /* =============================== 背单词屏 =============================== */
  var game = null;
  function wordsInit() {
    var min = 5;
    $$("#wordsDuration .seg-btn").forEach(function (b) {
      b.addEventListener("click", function () {
        $$("#wordsDuration .seg-btn").forEach(function (x) { x.classList.remove("is-on"); });
        b.classList.add("is-on"); min = parseInt(b.getAttribute("data-min"), 10);
      });
    });
    $("#wordsBankNote").textContent = "词库：" + (WORDS.length || (window.T2M_WORDS || []).length) + " 词（CET-4 高频）";
    $("#wordsStartBtn").addEventListener("click", function () { startGame(min); });
    $("#wordsAgain").addEventListener("click", function () {
      $("#wordsResult").style.display = "none"; $("#wordsStart").style.display = "";
    });
    $("#wordsToDash").addEventListener("click", function () { switchScreen("dash"); });
  }
  function pickQuestions(count) {
    var pool = WORDS.slice();
    // Fisher-Yates 洗牌
    for (var i = pool.length - 1; i > 0; i--) { var j = Math.floor(Math.random() * (i + 1)); var t = pool[i]; pool[i] = pool[j]; pool[j] = t; }
    var chosen = pool.slice(0, Math.min(count, pool.length));
    return chosen.map(function (w, idx) {
      var dir = idx % 3 === 2 ? "zh2en" : "en2zh";
      var others = pool.filter(function (x) { return x.zh !== w.zh && x.en !== w.en; });
      var distract = others.slice(0, 3).map(function (x) { return dir === "en2zh" ? x.zh : x.en; });
      while (distract.length < 3 && pool.length > 1) { var r = pool[Math.floor(Math.random() * pool.length)]; var v = dir === "en2zh" ? r.zh : r.en; if (v !== (dir === "en2zh" ? w.zh : w.en) && distract.indexOf(v) < 0) distract.push(v); }
      var correct = dir === "en2zh" ? w.zh : w.en;
      var opts = [correct].concat(distract.slice(0, 3));
      for (var k = opts.length - 1; k > 0; k--) { var m = Math.floor(Math.random() * (k + 1)); var tt = opts[k]; opts[k] = opts[m]; opts[m] = tt; }
      return { word: w, dir: dir, prompt: dir === "en2zh" ? (w.en + " 的中文意思是？") : ("「" + w.zh + "」对应的英文是？"), options: opts, answer: opts.indexOf(correct) };
    });
  }
  function startGame(minutes) {
    var count = Math.max(4, Math.min(60, Math.round(minutes * 6)));
    // 专注时长按真实计时上报（从开局到交卷），不再用「词数 ÷ 6」这种拍脑袋的估算
    game = { qs: pickQuestions(count), i: 0, correct: 0, results: [], startedAt: Date.now() };
    $("#wordsStart").style.display = "none"; $("#wordsResult").style.display = "none"; $("#wordsQuiz").style.display = "";
    renderQuestion();
  }
  function renderQuestion() {
    var q = game.qs[game.i];
    $("#qIdx").textContent = game.i + 1; $("#qTotal").textContent = game.qs.length;
    $("#qCorrect").textContent = game.correct;
    $("#qProgress").style.width = (game.i / game.qs.length * 100) + "%";
    $("#qPrompt").textContent = q.prompt;
    $("#qFeedback").textContent = "";
    var html = q.options.map(function (o, i) {
      return '<button class="quiz-opt" data-i="' + i + '"><span class="opt-key">' + "ABCD".charAt(i) + "</span>" + esc(o) + "</button>";
    }).join("");
    $("#qOpts").innerHTML = html;
    $$("#qOpts .quiz-opt").forEach(function (btn) {
      btn.addEventListener("click", function () { answer(parseInt(btn.getAttribute("data-i"), 10)); });
    });
  }
  function answer(choice) {
    var q = game.qs[game.i];
    var ok = choice === q.answer;
    if (ok) game.correct++;
    game.results.push({ en: q.word.en, correct: ok });
    $$("#qOpts .quiz-opt").forEach(function (b) { b.disabled = true; });
    var picked = $('#qOpts .quiz-opt[data-i="' + choice + '"]');
    if (picked) picked.classList.add(ok ? "right" : "wrong");
    if (!ok) { var right = $('#qOpts .quiz-opt[data-i="' + q.answer + '"]'); if (right) right.classList.add("right"); }
    $("#qFeedback").innerHTML = (ok ? "✓ 答对！" : "✗ 正确答案：" + esc(q.options[q.answer])) +
      ' <span class="q-en">' + esc(q.word.en) + " — " + esc(q.word.zh) + "</span>";
    game.i++;
    setTimeout(function () {
      if (game.i < game.qs.length) renderQuestion();
      else finishGame();
    }, 900);
  }
  function finishGame() {
    $("#wordsQuiz").style.display = "none"; $("#wordsResult").style.display = "";
    var total = game.qs.length, acc = Math.round(game.correct / total * 100);
    var color = acc >= 80 ? "var(--green)" : (acc >= 50 ? "var(--yellow-d)" : "var(--red)");
    $("#wordsResultBig").innerHTML = '<div class="rb-num" style="color:' + color + '">' + game.correct + "/" + total + "</div>" +
      '<div class="rb-sub">正确率 ' + acc + "% · 等餐时间没浪费</div>";
    var wrong = game.results.filter(function (r) { return !r.correct; })
      .map(function (r) { var w = WORDS.filter(function (x) { return x.en === r.en; })[0]; return w ? (w.en + " — " + w.zh) : r.en; });
    $("#wordsMistakes").innerHTML = wrong.length
      ? '<p class="note">下一局会优先复习这些词：</p><div class="mistake-list">' + wrong.map(function (w) { return "<span>" + esc(w) + "</span>"; }).join("") + "</div>"
      : '<p class="note">全对！这局没有错词 🎉</p>';
    var elapsedMin = Math.max(0.1, (Date.now() - (game.startedAt || Date.now())) / 60000);
    submit("play", "/api/play", { duration_min: Math.round(elapsedMin * 100) / 100, words: game.results })
      .then(function (r) {
        refreshStats();
        var tip = $("#wordsResultBig .rb-sub");
        if (!r && tip) tip.textContent = "已存到手机 · 回到同一个 WiFi 自动补传到电脑";
      });
  }

  /* =============================== 出发屏（我想去麦当劳） =============================== */
  /* 整个场景的起点：找最近门店 → 按目标挑这一餐 → 去官方下单 → 回来等餐背单词 */
  // mealPicked：用户手动选过餐次后就不再被「按时间自动推荐」覆盖
  var goState = { meal: "lunch", mealPicked: false, mode: "walk", orderUrl: "https://mcd.cn/", stores: [], origin: null };

  var SVC_LABEL = { dinein: "堂食", takeaway: "外带", drivethru: "得来速", delivery: "麦乐送", "24h": "24小时" };

  function goInit() {
    // 餐次切换（点击即为用户选择，之后不再被自动推荐覆盖）
    $$("#goMeal .seg-btn").forEach(function (b) {
      b.addEventListener("click", function () {
        goState.mealPicked = true;
        setMeal(b.getAttribute("data-meal"));
        $("#go-meal-note").textContent = "已手动选择" + (MEAL_LABEL[goState.meal] || goState.meal) + "，随时可切换。";
        loadPlan();
      });
    });
    // 通勤方式
    $("#go-mode").addEventListener("change", function () {
      goState.mode = this.value;
      $("#go-mode-tag").textContent = this.value === "bike" ? "骑行" : "步行";
      if (goState.stores.length) searchStores();   // 已查过就按新方式重算
    });
    $("#go-search").addEventListener("click", searchStores);
    $("#go-address").addEventListener("keydown", function (e) { if (e.key === "Enter") searchStores(); });
    $("#go-locate").addEventListener("click", locateMe);

    // 跳转官方下单（不调 create-order，零资金风险）；
    // 选过门店就带上那一趟的通勤消耗，下单后自动记账
    $("#goOrderBtn").addEventListener("click", function () {
      var picked = goState.stores.filter(function (s) { return s.picked; })[0] || goState.stores[0];
      if (picked) {
        startJourney(picked);
      } else {
        window.open(goState.orderUrl, "_blank", "noopener");
        $("#goOrderNote").textContent = "先查一下附近门店，下单后才会自动记录这一趟的消耗。";
      }
    });

    // 入口/跳转
    $("#dashGoBtn").addEventListener("click", function () { switchScreen("go"); });
    $("#heroWordsBtn").addEventListener("click", function () { switchScreen("words"); });
    $("#goEditProfile").addEventListener("click", function () { switchScreen("profile"); });
    $("#pf-back").addEventListener("click", function () { switchScreen("go"); });

    // 官方下单入口可从环境变量 T2M_ORDER_URL 覆盖
    fetchJSON("/api/config").then(function (c) {
      if (c && c.order_url) {
        goState.orderUrl = c.order_url;
        $("#goOrderNote").textContent = "跳转到 " + c.order_url + "（麦当劳官方渠道）";
      }
    }).catch(function () { /* 离线时保持默认官方地址 */ });

    applyDefaultMeal();   // 内部拿到时间后自己会 loadPlan()

    // ?autosearch=1&addr=xxx：进页面就自动查一次门店（截图 / 分享链接用）
    if (/[?&]autosearch=1/.test(location.search)) {
      var q = /[?&]addr=([^&]+)/.exec(location.search);
      if (q) $("#go-address").value = decodeURIComponent(q[1]);
      searchStores();
    }
  }

  /* 选中某个餐次：同步按钮高亮与右上角标签 */
  function setMeal(meal) {
    goState.meal = meal;
    $$("#goMeal .seg-btn").forEach(function (x) {
      x.classList.toggle("is-on", x.getAttribute("data-meal") === meal);
    });
    $("#go-meal-tag").textContent = MEAL_LABEL[meal] || meal;
  }

  /* 首次进入出发屏：按麦当劳服务端时间落到 早餐/午餐/晚餐/加餐 作为默认值 */
  function applyDefaultMeal() {
    fetchJSON("/api/now").then(function (d) {
      if (d && d.meal && !goState.mealPicked) setMeal(d.meal);
      if (d && d.time) {
        var src = d.source === "mcp" ? "麦当劳服务端时间" : "本机时间（MCP 未连上）";
        $("#go-meal-note").textContent =
          "现在 " + d.time + "（" + src + "），已按" + (d.meal_label || d.meal) + "推荐" +
          (d.window ? "（" + d.window + "）" : "") + "。想换直接点上面。";
      }
      loadPlan();
    }).catch(function () {
      $("#go-meal-note").textContent = "（离线）启动 python run.py web 后会按当前时间自动选餐次。";
      loadPlan();
    });
  }

  /* 浏览器定位：拿到坐标后回填「你在哪」，并按这个位置查门店 */
  function locateMe() {
    if (!navigator.geolocation) {
      $("#go-locate-note").textContent = "当前浏览器不支持定位，可按地址查询（路线将按距离估算）。";
      return;
    }
    $("#go-locate-note").textContent = "正在获取位置…（需要你授权一次）";
    navigator.geolocation.getCurrentPosition(
      function (pos) {
        var pt = [pos.coords.latitude, pos.coords.longitude];
        goState.origin = pt;
        $("#go-locate-note").textContent = "已拿到坐标 " + pt[0].toFixed(4) + ", " + pt[1].toFixed(4) +
          "，正在翻成地址…（仅本机使用）";
        // 坐标 → 地名，填回「你在哪」，再按它查附近门店
        fetchJSON("/api/reverse?lat=" + pt[0] + "&lng=" + pt[1]).then(function (d) {
          var addr = (d && d.address) || "";
          if (addr) {
            $("#go-address").value = addr;
            $("#go-locate-note").textContent = "你在哪已更新为「" + addr + "」" +
              "（" + pt[0].toFixed(4) + ", " + pt[1].toFixed(4) + "），路线按这个位置算。";
          } else {
            $("#go-locate-note").textContent =
              "坐标已用于路线计算，但没能翻成地址（" + esc((d && d.error) || "逆地址解析不可用") +
              "）。门店仍按你填的地址查找。";
          }
          searchStores();   // 用更新后的「你在哪」重新找店
        }).catch(function () {
          $("#go-locate-note").textContent = "坐标已用于路线计算（离线时无法翻成地址）。";
          if (goState.stores.length) searchStores();
        });
      },
      function (err) {
        var why = { 1: "你拒绝了定位授权", 2: "位置暂不可用", 3: "定位超时" }[err.code] || "定位失败";
        $("#go-locate-note").textContent = why + "，仍可按地址查询（路线按距离估算）。";
      },
      { enableHighAccuracy: true, timeout: 8000, maximumAge: 60000 }
    );
  }

  function searchStores() {
    var addr = ($("#go-address").value || "").trim();
    if (!addr) { $("#go-status").textContent = "先填个地址或地标"; return; }
    $("#go-status").textContent = "正在查找…";
    api("POST", "/api/stores", { address: addr, mode: goState.mode, limit: 3, origin: goState.origin || null }).then(function (d) {
      if (!d) { $("#go-status").textContent = "（离线）启动 python run.py web 后才能查门店"; return; }
      if (d.error) { $("#go-status").textContent = "查询失败：" + d.error; return; }
      goState.stores = d.stores || [];
      if (d.order_url) goState.orderUrl = d.order_url;
      renderStores(d);
      // 逆地址解析成功时，把「坐标」换成看得懂的地名；失败也不影响已有定位提示
      if (d.origin_label) {
        $("#go-locate-note").textContent = "已定位：" + d.origin_label + "（仅本机使用）";
      } else if (goState.origin && !d.origin_label) {
        $("#go-locate-note").textContent =
          "已定位：" + goState.origin[0].toFixed(4) + ", " + goState.origin[1].toFixed(4) +
          "（仅本机使用）";
      }
      var q = d.quota || {};
      var quotaTxt = q.available
        ? " · 地图配额 " + q.used_today + "/" + q.max_per_day + "（今日剩 " + q.left_today + "）"
        : "";
      $("#go-status").textContent = (d.demo ? "演示数据 · " : "") + "找到 " + goState.stores.length + " 家门店" + quotaTxt;
    });
  }

  function renderStores(d) {
    var rows = (d.stores || []).map(function (s, i) {
      var c = s.commute || {};
      var svc = (s.services || []).map(function (x) { return SVC_LABEL[x] || ""; }).filter(Boolean).join(" · ");
      return '<div class="store-card' + (i === 0 ? " is-best" : "") + '">' +
        '<div class="sc-top">' + (i === 0 ? '<span class="sc-badge">最近</span>' : "") +
          "<b>" + esc(s.name) + "</b></div>" +
        '<div class="sc-meta">' + esc(s.distance_text || "—") + " · " + esc(s.hours || "—") +
          (s.is_open ? ' <span class="sc-open">营业中</span>' : ' <span class="sc-closed">已打烊</span>') + "</div>" +
        (s.address ? '<div class="sc-addr">' + esc(s.address) + "</div>" : "") +
        (svc ? '<div class="sc-svc">' + esc(svc) + "</div>" : "") +
        (c.kcal != null
          ? '<div class="sc-commute">' + icon("shoe") + esc(c.mode_label || goState.mode) +
            " 约 " + fmt(c.minutes, 1) + " 分钟 · 消耗 <b>" + fmt(c.kcal, 1) + "</b> kcal" +
            (c.source === "estimate"
              ? '<span class="sc-est">（' + esc(c.note || "按距离估算，配 T2M_TMAP_KEY 后为真实路线") + '）</span>'
              : '<span class="sc-real">（真实路线' + (c.note ? " · " + esc(c.note) : "") + '）</span>') + "</div>"
          : "") +
        '<div class="sc-actions">' +
          '<button type="button" class="btn btn-red btn-sm" data-go-order="' + i + '">' +
            icon("burger") + " It's Time to Mc · 去这家下单</button>" +
        "</div>" +
        "</div>";
    }).join("");
    $("#goStores").innerHTML = rows || '<p class="note">没有找到符合条件的门店，换个地标再试。</p>';
    $$("[data-go-order]").forEach(function (b) {
      b.addEventListener("click", function () {
        var s = goState.stores[parseInt(b.getAttribute("data-go-order"), 10)];
        if (s) startJourney(s);
      });
    });
  }

  /* =============================== 一趟旅程：下单 → 追踪 → 自动记账 =============================== */
  /* 点「It's Time to Mc」后：跳官方下单，之后每 2 分钟查一次最新订单。
     一旦出现一个「之前没有的」新订单，就把这一趟的通勤消耗、这一单的营养、
     以及等餐时长一次性写进数据库（同一单只记一次）。 */
  var journeyTimer = null;
  var journey = null;

  function startJourney(store) {
    var c = store.commute || {};
    journey = {
      store: store.name || "",
      mode: c.mode_label || (goState.mode === "bike" ? "骑行" : "步行"),
      distance_m: c.distance_m || 0,
      minutes: c.minutes || 0,
      kcal: c.kcal || 0,
      source: c.source || "estimate",
      startedAt: Date.now(),
      baseline: null,     // 下单前的最新订单号，用来判断「这是不是新下的单」
      settled: false
    };
    window.open(goState.orderUrl, "_blank", "noopener");

    var note = $("#goOrderNote");
    if (note) {
      note.innerHTML = "已打开麦当劳官方下单页（" + esc(journey.store) + "）。" +
        "下完单回到这里，我们会每 2 分钟查一次最新订单，一出现新订单就自动把" +
        "<b>通勤消耗 " + fmt(journey.kcal, 1) + " kcal</b> 和这一餐的营养一起记进成长档案。";
    }

    // 先记住下单前的最新订单号
    fetchJSON("/api/order").then(function (d) {
      journey.baseline = (d && d.order && d.order.order_id) || "";
      watchNewOrder();
      if (journeyTimer) clearInterval(journeyTimer);
      journeyTimer = setInterval(watchNewOrder, 120000);   // 每 2 分钟
    }).catch(function () {
      watchNewOrder();
      journeyTimer = setInterval(watchNewOrder, 120000);
    });
  }

  function watchNewOrder() {
    if (!journey || journey.settled) return;
    fetchJSON("/api/order").then(function (d) {
      if (!journey || journey.settled || !d || !d.order) return;
      var o = d.order;
      if (!o.order_id || o.order_id === journey.baseline) return;   // 还是旧单
      // 真的是刚下的单：订单时间在点击之后（留 30 分钟宽限，防服务端时间差）
      var created = o.created_at ? Date.parse(o.created_at.replace(" ", "T")) : NaN;
      if (isFinite(created) && created < journey.startedAt - 30 * 60000) return;

      journey.settled = true;
      if (journeyTimer) { clearInterval(journeyTimer); journeyTimer = null; }
      settleJourney(o);
    }).catch(function () {});
  }

  function settleJourney(order) {
    var waitMin = Math.max(0.5, (Date.now() - journey.startedAt) / 60000);
    submit("meal", "/api/meal", {
      order_id: order.order_id,
      wait_min: Math.round(waitMin * 10) / 10,
      commute: {
        store: journey.store,
        mode: journey.mode,
        distance_m: journey.distance_m,
        minutes: journey.minutes,
        kcal: journey.kcal,
        source: journey.source === "estimate" ? "web-estimate" : "web-route"
      }
    }).then(function (r) {
      var note = $("#goOrderNote");
      if (!r || r.error) {
        if (note) note.textContent = "订单已识别，但记账失败：" + ((r && r.error) || "服务未连接") +
          "。可以去「记录」屏手动记入这一单。";
        return;
      }
      refreshStats();
      var t = r.totals || {};
      if (note) {
        note.innerHTML = "✓ 已把这趟记进成长档案：" + esc(r.store || journey.store) +
          " · " + esc(r.meal_type ? (MEAL_LABEL[r.meal_type] || r.meal_type) : "这一餐") +
          " <b>" + fmt(t.kcal) + " kcal</b> / 蛋白 " + fmt(t.protein, 1) + " g" +
          "；路上" + esc(journey.mode) + "消耗 <b>" + fmt(journey.kcal, 1) + " kcal</b>" +
          "；等餐 <b>" + fmt(waitMin, 1) + " 分钟</b>也计成了专注时间。";
      }
    });
  }

  function loadPlan() {
    api("POST", "/api/preorder", { meal: goState.meal, limit: 3 }).then(function (d) {
      var box = $("#goPlan");
      if (!d || d.error) {
        box.innerHTML = '<p class="note">' +
          esc((d && d.error) || "启动 python run.py web 后才能计算营养目标") + "</p>";
        return;
      }
      if (d.order_url) goState.orderUrl = d.order_url;
      var t = d.targets || {};
      var combos = (d.combos || []).map(function (c, i) {
        return '<div class="combo' + (i === 0 ? " is-top" : "") + '">' +
          '<div class="combo-h">' + (i === 0 ? '<span class="sc-badge">最贴近</span>' : "") +
            icon("burger") + esc(c.items.join(" + ")) + "</div>" +
          '<div class="combo-m">' +
            comboMacro("热量", c.kcal, t.kcal, "kcal") +
            comboMacro("蛋白", c.protein, t.protein, "g") +
            comboMacro("脂肪", c.fat, t.fat, "g") +
            comboMacro("碳水", c.carb, t.carb, "g") +
          "</div>" +
          ((c.reasons && c.reasons.length) ? '<div class="combo-why">' + esc(c.reasons[0]) + "</div>" : "") +
          (c.gaps && c.gaps.length ? '<div class="combo-gap">还差：' + esc(c.gaps[0]) + "</div>" : "") +
          "</div>";
      }).join("");
      box.innerHTML =
        '<div class="plan-target">这一餐目标：<b>' + fmt(t.kcal) + "</b> kcal · 蛋白 " + fmt(t.protein, 1) +
          " g · 脂肪 " + fmt(t.fat, 1) + " g · 碳水 " + fmt(t.carb, 1) + " g" +
          (d.demo ? ' <span class="tag">演示营养库</span>' : "") + "</div>" +
        (combos || '<p class="note">营养库为空，无法推荐。先跑 doctor 检查 list-nutrition-foods。</p>');
      $("#go-meal-tag").textContent = MEAL_LABEL[goState.meal] || goState.meal;
    });
  }
  function comboMacro(name, val, target, unit) {
    var pct = target ? Math.round(val / target * 100) : 0;
    return '<div class="combo-macro"><span>' + name + "</span><b>" + fmt(val, unit === "g" ? 1 : 0) +
      '</b><i>' + unit + "</i><em>" + pct + "%</em></div>";
  }

  /* =============================== 本餐屏 =============================== */
  function mealInit() {
    $("#mf-save").addEventListener("click", function () {
      var payload = {
        store: $("#mf-store").value || "麦当劳", meal_type: $("#mf-type").value,
        target_kcal: (DATA && DATA.profile && DATA.profile.targets) ? (DATA.profile.targets[$("#mf-type").value] || {}).kcal : null,
        items: [{ name: "本餐", qty: 1, kcal: parseFloat($("#mf-kcal").value) || 0, protein: parseFloat($("#mf-protein").value) || 0,
          fat: parseFloat($("#mf-fat").value) || 0, carb: parseFloat($("#mf-carb").value) || 0, sodium: parseFloat($("#mf-sodium").value) || 0 }]
      };
      submit("meal", "/api/meal", payload).then(function (resp) {
        $("#mf-status").textContent = resp ? "✓ 已记录，看板与对比已更新。" : "（离线）已存到手机，回到同一个 WiFi 会自动补传到电脑。";
        refreshStats(); renderMealScreen();
      });
    });
  }
  /* 记录屏：用麦当劳历史订单回填，避免只有手填的假数据 */
  var HIST = null;
  function loadOrderHistory() {
    var box = $("#mealOrders"), status = $("#mealOrdersStatus");
    fetchJSON("/api/history").then(function (d) {
      if (!d) {
        box.innerHTML = '<p class="note">（离线）启动 python run.py web 后才能读历史订单。</p>';
        return;
      }
      if (d.error) {
        box.innerHTML = '<p class="note">读取失败：' + esc(d.error) + "</p>";
        return;
      }
      HIST = d;
      renderMealScreen();   // 历史订单也能作为「目标 vs 实际」的来源
      var orders = d.orders || [];
      status.textContent = (d.demo ? "演示订单 · " : "") + orders.length + " 单";
      if (!orders.length) {
        box.innerHTML = '<p class="note">麦当劳账号里还没有历史订单，可以用右边表单手动记一餐。</p>';
        return;
      }
      box.innerHTML = orders.map(function (o) {
        var n = o.nutrition || {};
        var names = (o.items || []).map(function (i) { return i.name + "×" + i.qty; }).join(" + ") || "—";
        var est = (n.estimates || []).map(function (e) {
          return esc(e.name) + "→按" + esc(e.category_label || "同类") + " ¥" + fmt(e.based_on_price, 1) +
            " 的「" + esc(e.based_on) + "」估";
        }).join("；");
        var nutri = n.matched
          ? '<b>' + fmt(n.kcal) + '</b> kcal · 蛋白 ' + fmt(n.protein, 1) + " g · 脂肪 " + fmt(n.fat, 1) +
            " g · 碳水 " + fmt(n.carb, 1) + " g" +
            (n.matched < n.total ? '<span class="sc-est">（' + n.matched + "/" + n.total + " 项匹配到营养库）</span>" : "")
          : '<span class="sc-est">营养库里匹配不到这一单的商品，请手动记录</span>';
        if (est) nutri += '<div class="sc-est">估算：' + est + "</div>";
        return '<div class="store-card">' +
          '<div class="sc-top"><b>' + esc(o.store || "麦当劳") + "</b>" +
            '<span class="sc-badge">' + esc(o.stage_label || o.status_text || "") + "</span></div>" +
          '<div class="sc-meta">' + esc(o.time || "") + " · " + esc(o.meal_label || MEAL_LABEL[o.meal] || "这一餐") +
            (o.meal_window ? "（" + esc(o.meal_window) + "）" : "") +
            " · " + esc(o.mode_label || "") + (o.amount ? " · " + esc(o.amount) : "") + "</div>" +
          '<div class="sc-addr">' + esc(names) + "</div>" +
          '<div class="sc-commute">' + icon("burger") + nutri + "</div>" +
          '<div class="sc-actions">' +
            (o.logged
              ? '<span class="note">✓ 已记入成长档案</span>'
              : '<button type="button" class="btn btn-red btn-sm" data-log-order="' + esc(o.order_id) + '">记入这一餐</button>' +
                (n.matched ? '<button type="button" class="btn btn-sm" data-fill-order="' + esc(o.order_id) + '">填到下面表单</button>' : "")) +
          "</div></div>";
      }).join("");
      $$("[data-log-order]").forEach(function (b) {
        b.addEventListener("click", function () { logOrder(b.getAttribute("data-log-order"), b); });
      });
      $$("[data-fill-order]").forEach(function (b) {
        b.addEventListener("click", function () { fillFormFromOrder(b.getAttribute("data-fill-order")); });
      });
    }).catch(function () {
      box.innerHTML = '<p class="note">（离线）启动 python run.py web 后才能读历史订单。</p>';
    });
  }

  function logOrder(orderId, btn) {
    btn.disabled = true; btn.textContent = "记录中…";
    submit("meal", "/api/meal", { order_id: orderId }).then(function (r) {
      if (!r) {
        btn.disabled = false; btn.textContent = "已排队，回 WiFi 补传";
        return;
      }
      if (r.error) {
        btn.disabled = false; btn.textContent = "记入这一餐";
        $("#mealOrdersStatus").textContent = r.error;
        return;
      }
      refreshStats(); renderMealScreen(); loadOrderHistory();
    });
  }

  /* 把订单算出的营养填进手填表单，方便微调后再存 */
  function fillFormFromOrder(orderId) {
    fetchJSON("/api/history").then(function (d) {
      var o = ((d || {}).orders || []).filter(function (x) { return x.order_id === orderId; })[0];
      if (!o) return;
      var n = o.nutrition || {};
      $("#mf-store").value = o.store || "麦当劳";
      // 餐次由后端按订单时间 + coach.MEAL_WINDOWS 判定，和「出发」屏同一套规则，
      // 前端不再自己写一份判断（之前两份口径不一致）
      if (o.meal) $("#mf-type").value = o.meal;
      $("#mf-kcal").value = Math.round(n.kcal || 0);
      $("#mf-protein").value = n.protein || 0;
      $("#mf-fat").value = n.fat || 0;
      $("#mf-carb").value = n.carb || 0;
      $("#mf-sodium").value = n.sodium || 0;
      $("#mf-status").textContent = "已按订单 " + o.order_id.slice(-6) + " 填入，可改完再记录。";
    }).catch(function () {});
  }

  function renderMealScreen() {
    // 对比：优先用库里记录的最近一餐；一条都没记过时，退回麦当劳历史订单，
    // 这样刚装上也能立刻看到「目标 vs 实际」，而不是一片空白。
    var prof = DATA && DATA.profile;
    var targets = prof && prof.targets ? prof.targets : null;
    var meals = (DATA && DATA.recent && DATA.recent.meals) || [];
    var last = meals[0];
    var html = "";
    if (targets && last) {
      var t = targets[last.meal_type] || targets["lunch"];
      var actualK = last.kcal || 0, actualP = last.protein || 0;
      var tK = t.kcal || 1, tP = t.protein || 1;
      html = '<div class="cmp-head">' + (last.store || "麦当劳") + " · " +
        (MEAL_LABEL[last.meal_type] || last.meal_type) + " 目标对比</div>" +
        cmpBar("热量", actualK, tK, "kcal", "var(--red)") +
        cmpBar("蛋白质", actualP, tP, "g", "var(--green)") +
        '<p class="note">蛋白达成 ' + Math.round(actualP / tP * 100) + "% · 热量达成 " + Math.round(actualK / tK * 100) + "%（±15% 内算达标）。</p>";
    } else if (targets && HIST && HIST.orders && HIST.orders.length) {
      // 最近一单（跳过取消的），餐次由后端按订单时间 + 同一套时间规则判定
      var done = HIST.orders.filter(function (o) { return (o.status_text || "").indexOf("取消") < 0; });
      var o = done[0] || HIST.orders[0];
      var n = o.nutrition || {};
      var tg = targets[o.meal] || targets["lunch"];
      html = '<div class="cmp-head">' + esc(o.store || "麦当劳") + " · " +
        esc(o.meal_label || (MEAL_LABEL[o.meal] || "这一餐")) + " 目标对比" +
        '<span class="sc-est">（来自麦当劳历史订单 ' + esc(o.time || "") + "，还没记入档案）</span></div>" +
        cmpBar("热量", n.kcal || 0, tg.kcal || 1, "kcal", "var(--red)") +
        cmpBar("蛋白质", n.protein || 0, tg.protein || 1, "g", "var(--green)") +
        '<p class="note">蛋白达成 ' + Math.round((n.protein || 0) / (tg.protein || 1) * 100) +
        "% · 热量达成 " + Math.round((n.kcal || 0) / (tg.kcal || 1) * 100) + "%。" +
        (n.estimates && n.estimates.length
          ? "其中 " + n.estimates.length + " 项是按「同类价格最近」估算的（见下方订单卡）。"
          : "") + "</p>";
    } else if (targets) {
      html = '<p class="note">已有营养目标；记录一餐后这里会显示对比。</p>';
    } else {
      html = '<p class="note">先在「我的档案」算好营养目标，这里会把最近一餐的蛋白 / 热量与目标并排对比。</p>';
    }
    $("#mealCompare").innerHTML = html;
    // 最近餐时间线
    $("#mealTimeline").innerHTML = meals.length
      ? meals.slice(0, 6).map(function (m) {
          var items = []; try { items = JSON.parse(m.items_json || "[]"); } catch (e) {}
          var names = items.map(function (i) { return i.name + "×" + i.qty; }).join(" + ") || "—";
          return '<div class="tl-item ' + (m.on_target ? "ok" : "no") + '"><span class="tl-time">' + (m.ts || "").replace("T", " ").slice(5, 16) +
            '</span><span class="tl-main"><b>' + esc((m.store || "麦当劳") + " · " + ({ breakfast: "早餐", lunch: "午餐", dinner: "晚餐", snack: "加餐" }[m.meal_type] || m.meal_type)) +
            "</b><span>" + esc(names) + "</span></span><span class=\"tl-num\">" + fmt(m.kcal) + " kcal · " + fmt(m.protein, 1) + "g 蛋白</span></div>";
        }).join("")
      : '<p class="note">还没有餐记录。</p>';
  }
  function cmpBar(name, actual, target, unit, color) {
    var pctA = Math.min(100, Math.round(actual / target * 100));
    return '<div class="cmp-row"><div class="cmp-label">' + name + " <span>" + fmt(actual, unit === "g" ? 1 : 0) + " / " + fmt(target, unit === "g" ? 1 : 0) + " " + unit + "</span></div>" +
      '<div class="bar"><i class="bar-fill" data-cmp style="width:0%;background-color:' + color + '"></i></div></div>';
  }

  /* =============================== 导航 =============================== */
  function switchScreen(name) {
    $$(".nav-btn").forEach(function (b) { b.classList.toggle("is-on", b.getAttribute("data-screen") === name); });
    $$(".screen").forEach(function (s) { s.classList.toggle("is-on", s.id === "screen-" + name); });
    if (name === "words") startOrderPolling(); else stopOrderPolling();
    if (name === "go") loadPlan();   // 回到出发屏时按最新档案重算目标
    if (name === "meal") {
      renderMealScreen();
      loadOrderHistory();   // 拿到历史订单后会再刷新一次对比（库里没记录时用它兜底）
    }
    window.scrollTo(0, 0);
  }

  /* =============================== 订单状态（背单词屏实时轮询 4.2.13） =============================== */
  var orderTimer = null;

  function fmtOrderMin(v) {
    if (v == null || v === "") return "—";
    var n = Number(v);
    if (!isFinite(n)) return "—";
    if (n < 1) return "马上";
    return Math.round(n) + " 分钟";
  }
  function orderRow(name, val, color) {
    return '<div class="order-row"><span class="or-name">' + esc(name) + '</span><span class="or-val" style="color:' +
      (color || "var(--ink)") + '">' + (val == null || val === "" ? "—" : val) + '</span></div>';
  }
  function renderOrderPanel(data) {
    var connEl = $("#orderConn"), bodyEl = $("#orderBody"), updEl = $("#orderUpdated"), liveEl = $(".order-live i");
    if (!bodyEl) return;
    if (!data) { if (connEl) connEl.textContent = "未连接"; return; }

    var connText, connCls;
    if (data.error) { connText = "拉取失败"; connCls = "is-err"; }
    else if (data.connected && !data.demo) { connText = "MCP 已连接"; connCls = "is-ok"; }
    else if (data.demo && !data.connected) { connText = "演示模式"; connCls = "is-demo"; }
    else { connText = "未连接"; connCls = "is-err"; }
    if (connEl) { connEl.textContent = connText; connEl.className = "tag order-conn " + connCls; }
    if (liveEl) liveEl.className = "pulse-dot";

    if (!data.order) {
      var hint = data.demo
        ? "（演示模式：未配置 MCD_MCP_TOKEN，下方为模拟订单，非真实订单）"
        : (data.connected ? "暂时没有进行中的订单，点单后会自动出现。" : "未连接到麦当劳 MCP，配置 Token 后可查看真实订单。");
      bodyEl.innerHTML = '<div class="order-empty">' + icon("clock") +
        '<div><strong>暂无进行中的订单</strong><span class="note">' + hint + '</span></div></div>';
      if (updEl) updEl.textContent = "";
      return;
    }

    var o = data.order, eta = data.eta || {}, plan = data.plan || {};
    var items = (o.items || []).map(function (i) { return esc((i.name || "—") + " ×" + (i.qty || 1)); }).join(" + ");
    var stageColor = o.is_final ? "var(--green)" : "var(--red)";
    var confLabel = { high: "高", medium: "中", low: "低" }[eta.confidence] || "—";

    bodyEl.innerHTML =
      '<div class="order-head">' +
        '<div class="order-store">' + icon("burger") + esc(o.store_name || "麦当劳") + '</div>' +
        '<div class="order-mode">' + esc(o.mode_label || "到店取餐") + '</div>' +
      '</div>' +
      '<div class="order-stage" style="border-left-color:' + stageColor + '">' +
        '<div class="order-stage-l" style="color:' + stageColor + '">' + esc(o.stage_label || "") + '</div>' +
        '<div class="order-stage-t">' + esc(o.status_text || "") + '</div>' +
      '</div>' +
      '<div class="order-bar"><div class="bar"><i class="bar-fill" data-order-bar style="width:0%;background-color:' + (o.is_final ? "var(--green-l)" : "var(--red)") + '"></i></div>' +
        '<span class="order-pct">' + (o.progress || 0) + '%</span></div>' +
      '<div class="order-rows">' +
        orderRow("取餐码", o.pickup_code || "—", "var(--yellow-d)") +
        orderRow("预计等待", fmtOrderMin(eta.remaining_min), stageColor) +
        orderRow("预估信心", confLabel, "var(--ink)") +
        orderRow("订单金额", (o.amount != null && o.amount !== "") ? "¥" + fmt(o.amount, 2) : "—", "var(--ink)") +
      '</div>' +
      (items ? '<div class="order-items"><span class="oi-label">订单内容</span>' + items + '</div>' : "") +
      (plan.advice ? '<div class="order-advice ' + (plan.tone === "urgent" ? "urgent" : "") + '">' + icon("route") + '<span>' + esc(plan.advice) + '</span></div>' : "");

    setTimeout(function () {
      var bar = bodyEl.querySelector("[data-order-bar]");
      if (bar) bar.style.width = (o.progress || 0) + "%";
    }, 60);

    if (updEl) updEl.textContent = "订单号 " + esc(o.order_id || "—");
  }

  function startOrderPolling() {
    if (!window.fetch) return;
    stopOrderPolling();
    fetchJSON("/api/order").then(renderOrderPanel).catch(function () {
      var c = $("#orderConn"); if (c && c.textContent === "连接中…") c.textContent = "未连接";
    });
    orderTimer = setInterval(function () {
      fetchJSON("/api/order").then(renderOrderPanel).catch(function () {});
    }, 5000);
  }
  function stopOrderPolling() {
    if (orderTimer) { clearInterval(orderTimer); orderTimer = null; }
  }

  function refreshStats() {
    if (!window.fetch) return;
    fetchJSON("/api/stats").then(function (d) { DATA = d; afterData(); }).catch(function () {});
  }

  /* =============================== 启动 =============================== */
  function afterData() {
    renderTop(DATA); renderCards(DATA); renderChart(DATA, "words"); bindTabs(DATA);
    renderMacros(DATA); renderBadges(DATA); renderTimeline(DATA);
    profileInit(); goInit(); wordsInit(); mealInit(); syncInit();
  }

  function start() {
    // 装个离线壳：手机出了门（没 WiFi）也能打开页面背单词
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("sw.js").catch(function () {});
    }
    loadData(function () {
      if (!DATA) {
        $("main").insertAdjacentHTML("afterbegin", '<div class="panel"><h2>没有读到数据</h2><p class="note">先跑 <code>python tools/export_web.py</code> 生成 stats.js，或用 <code>python run.py web</code> 启动服务。</p></div>');
        return;
      }
      afterData();
      $$(".nav-btn").forEach(function (b) { b.addEventListener("click", function () { switchScreen(b.getAttribute("data-screen")); }); });
      // 支持 #go / #words / #meal / #profile 直达某一屏，方便分享与截图
      var hash = (location.hash || "").replace("#", "");
      if (hash && $("#screen-" + hash)) switchScreen(hash);
      window.addEventListener("hashchange", function () {
        var h = (location.hash || "").replace("#", "");
        if (h && $("#screen-" + h)) switchScreen(h);
      });
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
