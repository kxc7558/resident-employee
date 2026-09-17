/* 把对话台挂进业务系统页面。
 *
 * 用法（宿主页面加一行）：
 *
 *   <script src="http://127.0.0.1:8765/embed.js"
 *           data-token="宿主给对话台的共享令牌"
 *           data-target="#employee-panel"></script>
 *
 * data-target 不填就找 `#employee-panel`；找不到就不挂（不报错、不打扰宿主页面）。
 *
 * ## 为什么用 iframe 而不是直接注入脚本
 *
 * **样式隔离。** 宿主系统往往有一大堆全局 CSS（食堂那个页面三千多行），
 * 直接注入我们的样式和它的互相污染——而且这种问题只在别人那儿复现，很难查。
 * iframe 换来的是两边都省心。代价是首屏多一次加载，值。
 *
 * 高度靠 postMessage 自适应：对话台量完自己的内容高度报给宿主，宿主设 iframe 高度。
 * 不加这个的话，iframe 要么留一大片空白，要么把内容截掉。
 */
(function () {
  'use strict';

  var me = document.currentScript;
  if (!me) return;

  var target = document.querySelector(me.dataset.target || '#employee-panel');
  if (!target) {
    console.warn('[数字员工] 找不到挂载点 ' + (me.dataset.target || '#employee-panel'));
    return;
  }

  var base = new URL(me.src, window.location.href).origin;
  var src = base + '/';
  if (me.dataset.token) src += '?token=' + encodeURIComponent(me.dataset.token);

  var frame = document.createElement('iframe');
  frame.src = src;
  frame.title = '数字员工';
  frame.setAttribute('allow', 'clipboard-write');
  frame.style.cssText = [
    'width:100%',
    'height:560px',        // 首屏先给个高度，等对话台报上来再调
    'border:0',
    'display:block',
    'border-radius:12px',
    'background:transparent',
    'color-scheme:normal'
  ].join(';');
  target.appendChild(frame);

  // 只认这个 iframe 发来的高度，别被页面上别的 postMessage 带偏
  window.addEventListener('message', function (event) {
    if (event.source !== frame.contentWindow) return;
    var data = event.data || {};
    if (data.type !== 're-height') return;
    var height = parseInt(data.height, 10);
    if (!height || height < 200) return;   // 明显不合理的值直接忽略
    frame.style.height = Math.min(height, 1600) + 'px';
  });

  // 宿主卸载时把 iframe 摘掉，免得留个还在跑的对话台
  window.addEventListener('beforeunload', function () {
    if (frame.parentNode) frame.parentNode.removeChild(frame);
  });
})();
