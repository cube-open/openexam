/* 在线考试系统 - 界面交互
   只做四件事：移动端抽屉、后台侧栏收起、顶栏菜单、确认对话框。
   考试页不引入本文件（答题界面要绝对安静）。
   ------------------------------------------------------------------ */
(function () {
  'use strict';

  /* ---------- 移动端侧边抽屉（后台） ---------- */
  function initDrawer() {
    var sidebar = document.querySelector('.admin-sidebar');
    if (!sidebar) return;

    var toggle = document.createElement('button');
    toggle.className = 'drawer-toggle';
    toggle.type = 'button';
    toggle.setAttribute('aria-label', '打开导航');
    toggle.innerHTML = '<span></span><span></span><span></span>';
    document.body.appendChild(toggle);

    var scrim = document.createElement('div');
    scrim.className = 'drawer-scrim';
    document.body.appendChild(scrim);

    function open() {
      sidebar.classList.add('open');
      scrim.classList.add('show');
      toggle.classList.add('hidden');
      document.body.style.overflow = 'hidden';
    }
    function close() {
      sidebar.classList.remove('open');
      scrim.classList.remove('show');
      toggle.classList.remove('hidden');
      document.body.style.overflow = '';
    }
    toggle.addEventListener('click', open);
    scrim.addEventListener('click', close);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') close();
    });
    // 点导航项后自动收起（移动端）
    sidebar.addEventListener('click', function (e) {
      if (e.target.closest('a') && window.matchMedia('(max-width: 900px)').matches) close();
    });
  }

  /* ---------- 顶栏菜单（学生端窄屏） ---------- */
  function initTopbar() {
    var bar = document.querySelector('.topbar');
    if (!bar) return;
    var user = bar.querySelector('.topbar-user');
    if (!user) return;

    var btn = document.createElement('button');
    btn.className = 'topbar-menu-btn';
    btn.type = 'button';
    btn.setAttribute('aria-label', '菜单');
    btn.innerHTML = '<span></span><span></span><span></span>';
    bar.insertBefore(btn, user);

    btn.addEventListener('click', function () {
      var open = user.classList.toggle('open');
      btn.classList.toggle('active', open);
    });
    document.addEventListener('click', function (e) {
      if (!user.contains(e.target) && !btn.contains(e.target)) {
        user.classList.remove('open');
        btn.classList.remove('active');
      }
    });
  }

  /* ---------- 表单确认（危险操作二次确认） ---------- */
  function initConfirm() {
    document.addEventListener('submit', function (e) {
      var form = e.target;
      if (form.dataset.confirmed) return;
      var msg = form.getAttribute('data-confirm');
      if (!msg) return;
      if (!window.confirm(msg)) {
        e.preventDefault();
        e.stopPropagation();
      } else {
        form.dataset.confirmed = '1';
      }
    }, true);
  }

  /* ---------- 后台侧栏收起/展开（桌面端，记忆偏好） ---------- */
  var SIDEBAR_KEY = 'admin-sidebar-hidden';

  function restoreSidebarPref() {
    var sidebar = document.querySelector('.admin-sidebar');
    if (!sidebar) return;
    try {
      if (localStorage.getItem(SIDEBAR_KEY) === '1') {
        document.body.classList.add('sidebar-hidden');
      }
    } catch (e) { /* 隐私模式等场景下静默失败 */ }
  }

  function initSidebarToggle() {
    var sidebar = document.querySelector('.admin-sidebar');
    if (!sidebar) return;
    var collapse = sidebar.querySelector('.sidebar-collapse');
    if (!collapse) return;

    var expand = document.createElement('button');
    expand.type = 'button';
    expand.className = 'sidebar-expand';
    expand.setAttribute('aria-label', '展开侧栏');
    expand.innerHTML = '<span class="chev">»</span> 展开侧栏';
    document.body.appendChild(expand);

    function setHidden(hidden) {
      document.body.classList.toggle('sidebar-hidden', hidden);
      try { localStorage.setItem(SIDEBAR_KEY, hidden ? '1' : '0'); } catch (e) {}
    }
    collapse.addEventListener('click', function () { setHidden(true); });
    expand.addEventListener('click', function () { setHidden(false); });
  }

  /* 尽早恢复偏好：defer 脚本在首次绘制前执行，避免侧栏闪一下 */
  restoreSidebarPref();

  document.addEventListener('DOMContentLoaded', function () {
    initDrawer();
    initTopbar();
    initConfirm();
    initSidebarToggle();
  });
})();
