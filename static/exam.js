/* 考试页逻辑：计时、全屏建议、切屏检测、即时保存 + 定时上传、自定义弹窗 */
(function () {
    const CFG = window.EXAM_CONFIG;
    const LS_KEY = 'exam_answers_' + CFG.examId;

    const timerEl = document.getElementById('timer');
    const violationEl = document.getElementById('violation');
    const formEl = document.getElementById('exam-form');
    const fsBanner = document.getElementById('fs-banner');
    const btnFs = document.getElementById('btn-fs');
    const btnFsClose = document.getElementById('btn-fs-close');
    const btnFullscreen = document.getElementById('btn-fullscreen');
    const btnSubmit = document.getElementById('btn-submit');
    const violationToast = document.getElementById('violation-toast');
    const violationToastMsg = document.getElementById('violation-toast-msg');
    const violationCountEl = document.getElementById('violation-count');
    const sheetGrid = document.getElementById('sheet-grid');
    const sheetProgress = document.getElementById('sheet-progress');
    const answeredProgress = document.getElementById('answered-progress');
    const allQuestions = Array.from(document.querySelectorAll('.question'));

    // 自定义弹窗（替代 alert/confirm，不打破全屏）
    const modalOverlay = document.getElementById('modal-overlay');
    const modalTitle = document.getElementById('modal-title');
    const modalMsg = document.getElementById('modal-msg');
    const modalInput = document.getElementById('modal-input');
    const modalOk = document.getElementById('modal-ok');
    const modalCancel = document.getElementById('modal-cancel');

    let remaining = CFG.remaining;
    let violationCount = CFG.violations;
    let submitted = false;
    let expired = false;
    let timerInterval = null;
    let lastViolationAt = 0;
    let saveTimer = null;

    // ---------- 弹窗 ----------
    function showModal(opts) {
        modalTitle.textContent = opts.title || '提示';
        modalMsg.textContent = opts.msg || '';
        modalOk.textContent = opts.okText || '确定';
        if (opts.cancelText) {
            modalCancel.style.display = '';
            modalCancel.textContent = opts.cancelText;
        } else {
            modalCancel.style.display = 'none';
        }
        // 输入框
        if (opts.inputPlaceholder) {
            modalInput.style.display = '';
            modalInput.placeholder = opts.inputPlaceholder;
            modalInput.value = '';
            modalOk.disabled = true;
            modalOk.className = 'btn ' + (opts.danger ? 'danger' : 'primary') + ' disabled';
            modalInput.oninput = function () {
                var match = modalInput.value === (opts.inputMatch || '');
                modalOk.disabled = !match;
                modalOk.className = 'btn ' + (opts.danger ? 'danger' : 'primary') + (match ? '' : ' disabled');
            };
            setTimeout(function () { modalInput.focus(); }, 100);
        } else {
            modalInput.style.display = 'none';
            modalOk.disabled = false;
        }
        modalOk.className = 'btn ' + (opts.danger ? 'danger' : 'primary');
        modalOverlay.classList.add('show');
        modalOk.onclick = function () {
            if (modalOk.disabled) return;
            modalOverlay.classList.remove('show');
            if (opts.onOk) opts.onOk();
        };
        modalCancel.onclick = function () { modalOverlay.classList.remove('show'); };
    }
    function showToast(msg) {
        violationToastMsg.textContent = msg;
        violationToast.classList.add('show');
        clearTimeout(violationToast._t);
        violationToast._t = setTimeout(function () { violationToast.classList.remove('show'); }, 3000);
    }

    // ---------- 计时 ----------
    function fmt(s) {
        s = Math.max(0, s);
        const m = Math.floor(s / 60), sec = s % 60;
        return String(m).padStart(2, '0') + ':' + String(sec).padStart(2, '0');
    }
    function updateTimerStyle() {
        if (remaining < 300) {
            timerEl.style.color = '#ef4444';
            timerEl.style.fontWeight = '700';
        } else {
            timerEl.style.color = '';
            timerEl.style.fontWeight = '';
        }
    }
    function startTimer() {
        timerEl.textContent = fmt(remaining);
        updateTimerStyle();
        timerInterval = setInterval(function () {
            remaining -= 1;
            timerEl.textContent = fmt(remaining);
            updateTimerStyle();
            if (remaining <= 0) {
                clearInterval(timerInterval);
                doSubmit(true);
            }
        }, 1000);
    }

    // ---------- 答案收集 / 保存 ----------
    function collectAnswers() {
        const answers = {};
        document.querySelectorAll('.question').forEach(function (q) {
            const id = q.dataset.id;
            const type = q.dataset.type;
            if (type === 'choice' || type === 'judge') {
                const c = q.querySelector('input[type="radio"]:checked');
                if (c) answers[id] = c.value;
            } else if (type === 'multi') {
                const checked = q.querySelectorAll('input[type="checkbox"]:checked');
                if (checked.length) {
                    answers[id] = Array.from(checked).map(function (c) { return c.value; }).sort().join('');
                }
            } else {
                const inp = q.querySelector('input.blank-input');
                if (inp && inp.value.trim()) answers[id] = inp.value.trim();
            }
        });
        return answers;
    }

    function saveLocal() {
        try { localStorage.setItem(LS_KEY, JSON.stringify(collectAnswers())); } catch (e) {}
    }
    function loadLocal() {
        try {
            const raw = localStorage.getItem(LS_KEY);
            return raw ? JSON.parse(raw) : {};
        } catch (e) { return {}; }
    }
    function applyAnswers(answers) {
        Object.keys(answers).forEach(function (id) {
            const q = document.querySelector('.question[data-id="' + id + '"]');
            if (!q) return;
            const val = answers[id];
            const type = q.dataset.type;
            if (type === 'choice' || type === 'judge') {
                const radio = q.querySelector('input[value="' + val + '"]');
                if (radio) radio.checked = true;
            } else if (type === 'multi') {
                String(val).split('').forEach(function (letter) {
                    const cb = q.querySelector('input[value="' + letter + '"]');
                    if (cb) cb.checked = true;
                });
            } else {
                const inp = q.querySelector('input.blank-input');
                if (inp) inp.value = val;
            }
        });
    }

    // ---------- 答题卡 ----------
    function isAnswered(q) {
        const type = q.dataset.type;
        if (type === 'multi') {
            return q.querySelectorAll('input[type="checkbox"]:checked').length > 0;
        } else if (type === 'choice' || type === 'judge') {
            return !!q.querySelector('input[type="radio"]:checked');
        } else {
            const inp = q.querySelector('input.blank-input');
            return !!(inp && inp.value.trim());
        }
    }

    // 标记题目（本地保存，答题卡可筛选）
    const MARK_KEY = 'exam_mark_' + CFG.examId;
    let marked = new Set();
    try { marked = new Set(JSON.parse(localStorage.getItem(MARK_KEY) || '[]')); } catch (e) { marked = new Set(); }

    function applyMarkFilter() {
        const onlyMarked = document.getElementById('filter-marked').checked;
        allQuestions.forEach(function (q, i) {
            const b = sheetGrid.children[i];
            if (b) b.style.display = (onlyMarked && !marked.has(q.dataset.id)) ? 'none' : '';
        });
    }
    function syncMarkUI() {
        allQuestions.forEach(function (q) {
            q.classList.toggle('marked', marked.has(q.dataset.id));
        });
        document.querySelectorAll('.q-mark').forEach(function (btn) {
            btn.classList.toggle('active', marked.has(btn.dataset.id));
        });
    }
    function toggleMark(id, btn) {
        if (marked.has(id)) marked.delete(id); else marked.add(id);
        try { localStorage.setItem(MARK_KEY, JSON.stringify(Array.from(marked))); } catch (e) {}
        syncMarkUI();
        updateSheet();
    }
    document.querySelectorAll('.q-mark').forEach(function (btn) {
        btn.addEventListener('click', function () { toggleMark(btn.dataset.id, btn); });
    });
    document.getElementById('filter-marked').addEventListener('change', applyMarkFilter);

    function buildSheet() {
        sheetGrid.innerHTML = '';
        allQuestions.forEach(function (q, i) {
            const b = document.createElement('button');
            b.type = 'button';
            b.className = 'sheet-num';
            b.textContent = i + 1;
            b.addEventListener('click', function () {
                q.scrollIntoView({ behavior: 'smooth', block: 'center' });
            });
            sheetGrid.appendChild(b);
        });
        updateSheet();
    }
    function updateSheet() {
        let answered = 0;
        allQuestions.forEach(function (q, i) {
            const a = isAnswered(q);
            if (a) answered++;
            const b = sheetGrid.children[i];
            if (b) {
                b.classList.toggle('answered', a);
                b.classList.toggle('marked', marked.has(q.dataset.id));
            }
        });
        const total = allQuestions.length;
        sheetProgress.textContent = '已答 ' + answered + ' / ' + total;
        answeredProgress.textContent = '已答 ' + answered + ' / ' + total;
        applyMarkFilter();
    }
    function uploadAnswers() {
        if (submitted) return;
        fetch(CFG.saveUrl, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ exam_id: CFG.examId, answers: collectAnswers() })
        }).then(function (r) { return r.json().catch(function () { return {}; }); })
          .then(function (d) {
              // 服务端判定时间已到：立刻交卷，输入框锁死
              if (d && d.expired) handleExpired();
              else if (d && d.closed && d.redirect) { lockForm(); window.location.href = d.redirect; }
          })
          .catch(function () {});
    }

    // 恢复：优先本地（最新未同步），兜底服务端已保存
    applyAnswers(CFG.savedAnswers || {});
    const local = loadLocal();
    if (Object.keys(local).length) applyAnswers(local);

    // 绑定输入事件：即时写本地 + 防抖上传
    formEl.addEventListener('change', function () {
        saveLocal();
        updateSheet();
        clearTimeout(saveTimer);
        saveTimer = setTimeout(uploadAnswers, 2500);
    });
    formEl.addEventListener('input', function () {
        saveLocal();
        updateSheet();
        clearTimeout(saveTimer);
        saveTimer = setTimeout(uploadAnswers, 2500);
    });

    // 定时上传（15 秒）
    setInterval(uploadAnswers, 15000);

    // 心跳（在线检测 + 离线切屏同步）
    function heartbeat() {
        if (submitted) return;
        fetch(CFG.heartbeatUrl, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ exam_id: CFG.examId, violations: violationCount })
        }).then(function (r) { return r.json(); })
          .then(function (d) {
              if (d.ok && d.violations !== undefined) {
                  violationCount = d.violations;
                  updateViolation();
              }
              // 服务端说时间已到：立刻交卷，别再等了
              if (d.expired) { handleExpired(); return; }
              // 同步剩余时间（老师修改时间后）
              if (d.remaining !== undefined && d.remaining > 0) {
                  remaining = d.remaining;
                  timerEl.textContent = fmt(remaining);
                  updateTimerStyle();
              }
              // 处理监考消息
              if (d.messages && d.messages.length) {
                  d.messages.forEach(function (msg) {
                      showModal({
                          title: '老师消息',
                          msg: msg.content,
                          okText: '收到'
                      });
                  });
              }
          })
          .catch(function () {});
    }
    heartbeat();
    setInterval(heartbeat, 8000);

    // 离开 / 隐藏时尽力保存
    window.addEventListener('beforeunload', function () {
        try {
            const blob = new Blob([JSON.stringify({ exam_id: CFG.examId, answers: collectAnswers() })],
                { type: 'application/json' });
            navigator.sendBeacon(CFG.saveUrl, blob);
        } catch (e) {}
    });
    document.addEventListener('visibilitychange', function () {
        if (!document.hidden) return;
        saveLocal();
        uploadAnswers();
    });

    // ---------- 全屏（建议，不阻塞） ----------
    const ICON_FS = '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3H5a2 2 0 0 0-2 2v3"></path><path d="M21 8V5a2 2 0 0 0-2-2h-3"></path><path d="M3 16v3a2 2 0 0 0 2 2h3"></path><path d="M16 21h3a2 2 0 0 0 2-2v-3"></path></svg>';
    const ICON_MIN = '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3v3a2 2 0 0 1-2 2H3"></path><path d="M21 8h-3a2 2 0 0 1-2-2V3"></path><path d="M3 16h3a2 2 0 0 1 2 2v3"></path><path d="M16 21v-3a2 2 0 0 1 2-2h3"></path></svg>';
    function setFsBtn(inFs) {
        btnFullscreen.innerHTML = (inFs ? ICON_MIN : ICON_FS) + '<span>' + (inFs ? '退出全屏' : '全屏') + '</span>';
    }
    function isFs() { return !!document.fullscreenElement; }
    function enterFs() {
        const el = document.documentElement;
        if (el.requestFullscreen) el.requestFullscreen().catch(function () {});
        else if (el.webkitRequestFullscreen) el.webkitRequestFullscreen();
    }
    function exitFs() {
        if (document.exitFullscreen) document.exitFullscreen().catch(function () {});
    }
    btnFs.addEventListener('click', enterFs);
    btnFsClose.addEventListener('click', function () { fsBanner.style.display = 'none'; });
    btnFullscreen.addEventListener('click', function () { isFs() ? exitFs() : enterFs(); });

    document.addEventListener('fullscreenchange', function () {
        if (isFs()) {
            fsBanner.style.display = 'none';
            setFsBtn(true);
        } else {
            setFsBtn(false);
            if (!submitted) {
                reportViolation('退出全屏');
                fsBanner.style.display = '';
            }
        }
    });

    // ---------- 切屏检测 ----------
    function updateViolation(pulse) {
        violationCountEl.textContent = '切屏警告 ' + violationCount + ' 次';
        if (pulse) {
            violationEl.classList.remove('pulse');
            void violationEl.offsetWidth;
            violationEl.classList.add('pulse');
        }
        checkEgg();
    }

    // 处罚彩蛋：切屏达到阈值时触发，只弹一次（本地记住，下次不再弹）
    const EGG_KEY = 'exam_egg_' + CFG.examId;
    let eggShown = false;
    try { eggShown = localStorage.getItem(EGG_KEY) === '1'; } catch (e) {}

    function checkEgg() {
        if (CFG.enableEgg && violationCount >= CFG.alertThreshold && !eggShown) {
            eggShown = true;
            try { localStorage.setItem(EGG_KEY, '1'); } catch (e) {}
            // 进入全屏后显示彩蛋
            var overlay = document.getElementById('egg-overlay');
            var el = document.documentElement;
            if (el.requestFullscreen) {
                el.requestFullscreen().then(function () {
                    overlay.classList.add('show');
                }).catch(function () {
                    overlay.classList.add('show');
                });
            } else {
                overlay.classList.add('show');
            }
        }
    }
    window.addEventListener('message', function (e) {
        if (e.data === 'egg-close') {
            document.getElementById('egg-overlay').classList.remove('show');
            // 退出全屏
            if (document.fullscreenElement && document.exitFullscreen) {
                document.exitFullscreen().catch(function () {});
            }
        }
    });
    // ---------- 禁用检测（URL 参数 ?nodetect=1） ----------
    function isDetectDisabled() { return !!CFG.nodetect; }

    // ---------- 清除缓存 ----------
    const btnClearCache = document.getElementById('btn-clear-cache');
    if (btnClearCache) {
        btnClearCache.addEventListener('click', function () {
            showModal({
                title: '清除本地答题缓存',
                msg: '此操作会清除浏览器中保存的所有答案，页面将刷新。请输入"我确定"以继续：',
                inputPlaceholder: '请输入"我确定"',
                inputMatch: '我确定',
                okText: '清除', cancelText: '取消', danger: true,
                onOk: function () {
                    try { localStorage.removeItem(LS_KEY); } catch (e) {}
                    location.reload();
                }
            });
        });
    }

    function reportViolation(reason) {
        if (submitted) return;
        if (isDetectDisabled()) return;
        const now = Date.now();
        if (now - lastViolationAt < 1500) return;
        lastViolationAt = now;
        fetch(CFG.violationUrl, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ exam_id: CFG.examId })
        }).then(function (r) { return r.json(); })
          .then(function (d) {
              if (!d.ok) return;
              violationCount = d.violations;
              updateViolation(true);
              showModal({ title: '检测到切屏', msg: '检测到切屏（' + reason + '），已记录警告 ' + d.violations + ' 次', okText: '知道了' });
          })
          .catch(function () {
              // 离线：本地先 +1，心跳重连后同步到服务器
              violationCount += 1;
              updateViolation(true);
              showModal({ title: '检测到切屏', msg: '检测到切屏（' + reason + '），已记录警告 ' + violationCount + ' 次', okText: '知道了' });
          });
    }

    document.addEventListener('visibilitychange', function () {
        if (document.hidden) reportViolation('切换标签页 / 最小化');
    });
    window.addEventListener('blur', function () {
        reportViolation('离开考试窗口');
    });

    // 考试期间禁用右键、复制、剪切、粘贴、选中、拖拽
    ['contextmenu', 'copy', 'cut', 'paste', 'selectstart', 'dragstart'].forEach(function (evt) {
        document.addEventListener(evt, function (e) { e.preventDefault(); });
    });

    // 检测截图（打印键）
    function onShot(e) {
        if (e.key === 'PrintScreen' || e.keyCode === 44 || e.key === 'SysReq') {
            e.preventDefault();
            reportViolation('疑似截图');
        }
    }
    document.addEventListener('keydown', onShot);
    document.addEventListener('keyup', onShot);

    // ---------- 交卷 ----------
    // 服务端判定考试时间已到：锁死表单并自动交卷。
    // 前端计时器只是提示，真正的约束在服务端，这里只是配合它收尾。
    function lockForm() {
        try {
            formEl.querySelectorAll('input, textarea, select').forEach(function (el) {
                el.disabled = true;
            });
        } catch (e) {}
    }
    function handleExpired() {
        if (submitted || expired) return;
        expired = true;
        remaining = 0;
        clearInterval(timerInterval);
        timerEl.textContent = '00:00';
        lockForm();
        doSubmit(true);
    }

    function doSubmit(auto) {
        if (submitted) return;
        submitted = true;
        clearInterval(timerInterval);
        const answers = collectAnswers();
        fetch(CFG.submitUrl, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ answers: answers, auto: !!auto })
        }).then(function (r) { return r.json(); })
          .then(function (d) {
              if (d.redirect) {
                  try { localStorage.removeItem(LS_KEY); } catch (e) {}
                  if (document.fullscreenElement && document.exitFullscreen) {
                      document.exitFullscreen().catch(function () {});
                  }
                  window.location.href = d.redirect;
              } else {
                  submitted = false;
                  showModal({ title: '交卷失败', msg: d.msg || '请稍后重试。' });
              }
          })
          .catch(function () {
              submitted = false;
              showModal({ title: '交卷失败', msg: '网络异常，请重试。' });
          });
    }

    btnSubmit.addEventListener('click', function () {
        const unanswered = document.querySelectorAll('.question').length - Object.keys(collectAnswers()).length;
        showModal({
            title: '确认交卷',
            msg: (unanswered > 0 ? '还有 ' + unanswered + ' 道题未作答。' : '全部题目已作答。') + '交卷后无法修改，确定吗？',
            okText: '交卷', cancelText: '再检查一下', danger: true,
            onOk: function () { doSubmit(false); }
        });
    });

    // 功德木鱼彩蛋
    const gongdeBtn = document.getElementById('gongde-btn');
    if (gongdeBtn) {
        const gongdeCountEl = document.getElementById('gongde-count');
        const GONGDE_KEY = 'gongde_' + CFG.examId;
        let gongdeCount = 0;
        try { gongdeCount = parseInt(localStorage.getItem(GONGDE_KEY) || '0', 10) || 0; } catch (e) {}
        if (gongdeCountEl) gongdeCountEl.textContent = '功德 ' + gongdeCount;

        let audioCtx = null;
        function playKnock() {
            try {
                if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
                const t = audioCtx.currentTime;
                const mk = function (type, freq, to, vol, dur) {
                    const o = audioCtx.createOscillator();
                    const g = audioCtx.createGain();
                    o.type = type;
                    o.frequency.setValueAtTime(freq, t);
                    o.frequency.exponentialRampToValueAtTime(to, t + dur);
                    g.gain.setValueAtTime(vol, t);
                    g.gain.exponentialRampToValueAtTime(0.001, t + dur);
                    o.connect(g); g.connect(audioCtx.destination);
                    o.start(t); o.stop(t + dur);
                };
                mk('sine', 160, 55, 0.8, 0.28);      // 低沉主体
                mk('triangle', 320, 110, 0.25, 0.14); // 高频泛音
            } catch (e) {}
        }

        gongdeBtn.addEventListener('click', function () {
            playKnock();
            gongdeBtn.classList.remove('hit');
            void gongdeBtn.offsetWidth;
            gongdeBtn.classList.add('hit');
            gongdeCount += 1;
            if (gongdeCountEl) gongdeCountEl.textContent = '功德 ' + gongdeCount;
            try { localStorage.setItem(GONGDE_KEY, String(gongdeCount)); } catch (e) {}
            const span = document.createElement('span');
            span.className = 'gongde-plus';
            span.textContent = '+1 功德';
            document.body.appendChild(span);
            const rect = gongdeBtn.getBoundingClientRect();
            span.style.left = (rect.left + rect.width / 2 - 32) + 'px';
            span.style.top = (rect.top - 10) + 'px';
            setTimeout(function () { span.remove(); }, 1200);
        });
    }

    // ---------- 启动 ----------
    updateViolation();
    setFsBtn(false);
    syncMarkUI();
    buildSheet();
    startTimer();
})();
