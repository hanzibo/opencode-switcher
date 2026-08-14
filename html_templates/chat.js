const KATEX_DELIMITERS = [
                    {left: '$$', right: '$$', display: true},
                    {left: '$', right: '$', display: false},
                    {left: '\\(', right: '\\)', display: false},
                    {left: '\\[', right: '\\]', display: true}
                ];
                window.__isStreamingVal = false;
                Object.defineProperty(window, '_isStreaming', {
                    get: function() {
                        return window.__isStreamingVal;
                    },
                    set: function(val) {
                        window.__isStreamingVal = val;
                        if (val) {
                            document.body.classList.add('streaming');
                        } else {
                            document.body.classList.remove('streaming');
                        }
                    }
                });

                let lightboxScale = 1.0;
                let translateX = 0;
                let translateY = 0;
                let isDragging = false;

                let startX = 0, startY = 0;
                let currentX = 0, currentY = 0;
                let dragDistance = 0;
                let rafId = null;

                // ── Streaming v2: 增量纯文本追加 ──
                let _streamingTextNode = null;
                let _streamingContainerId = null;

                // ── Reasoning 状态机（无计时器版） ──
                // 状态: 'idle' | 'thinking' | 'complete'
                let _reasoningState = 'idle';
                let _reasoningCache = '';           // 缓存的推理文本（展开时懒渲染）
                let _reasoningPendingText = '';      // 尚未 flush 的推理增量

                // ── Phase 2: Performance helpers ──
                let _mathDebounceTimer = null;
                let _windowingRafId = null;

                function _debouncedRenderMath(element) {
                    if (_mathDebounceTimer) clearTimeout(_mathDebounceTimer);
                    if (!window._isStreaming) {
                        _renderMath(element);
                        return;
                    }
                    _mathDebounceTimer = setTimeout(() => {
                        _renderMath(element);
                        _mathDebounceTimer = null;
                    }, 800);
                }

                function _throttledWindowing() {
                    if (_windowingRafId) return;
                    _windowingRafId = requestAnimationFrame(() => {
                        _windowingRafId = null;
                        applyWindowing();
                    });
                }

                document.addEventListener('DOMContentLoaded', function() {
                    if (typeof renderMathInElement === 'function') {
                        renderMathInElement(document.body, {
                            delimiters: KATEX_DELIMITERS,
                            throwOnError: false,
                            errorColor: 'transparent'
                        });
                    }

                    const lightbox = document.getElementById('lightbox');
                    const img = document.getElementById('lightbox-img');

                    function updateTransform() {
                        translateX = currentX;
                        translateY = currentY;
                        img.style.transform = `translate(${translateX}px, ${translateY}px) scale(${lightboxScale})`;
                        rafId = null;
                    }

                    if (lightbox && img) {
                        // Prevent system default image drag ghost image
                        img.addEventListener('dragstart', function(e) {
                            e.preventDefault();
                        });

                        // Double click to reset zoom & translation
                        img.addEventListener('dblclick', function(e) {
                            e.stopPropagation();
                            if (rafId) {
                                cancelAnimationFrame(rafId);
                                rafId = null;
                            }
                            lightboxScale = 1.0;
                            translateX = 0;
                            translateY = 0;
                            img.style.transform = 'translate(0px, 0px) scale(1)';
                        });

                        // Wheel Zoom
                        lightbox.addEventListener('wheel', function(e) {
                            e.preventDefault();
                            const zoomStep = 0.08;
                            if (e.deltaY < 0) {
                                lightboxScale = Math.min(lightboxScale + zoomStep, 5.0);
                            } else {
                                lightboxScale = Math.max(lightboxScale - zoomStep, 0.5);
                            }
                            img.style.transform = `translate(${translateX}px, ${translateY}px) scale(${lightboxScale})`;
                        }, { passive: false });

                        // Mouse Drag
                        lightbox.addEventListener('mousedown', function(e) {
                            if (e.button !== 0) return; // Only left button
                            isDragging = true;
                            startX = e.clientX - translateX;
                            startY = e.clientY - translateY;
                            dragDistance = 0;
                            lightbox.style.cursor = 'grabbing';
                            img.classList.add('dragging');
                        });

                        window.addEventListener('mousemove', function(e) {
                            if (!isDragging) return;
                            const nextX = e.clientX - startX;
                            const nextY = e.clientY - startY;
                            dragDistance += Math.abs(nextX - translateX) + Math.abs(nextY - translateY);
                            currentX = nextX;
                            currentY = nextY;
                            translateX = nextX;
                            translateY = nextY;
                            img.style.transform = `translate(${translateX}px, ${translateY}px) scale(${lightboxScale})`;
                        });

                        window.addEventListener('mouseup', function(e) {
                            if (!isDragging) return;
                            isDragging = false;
                            lightbox.style.cursor = '';
                            img.classList.remove('dragging');
                        });

                        // Click handler to close (only on background clicked)
                        lightbox.addEventListener('click', function(e) {
                            if (dragDistance > 8) return;
                            if (e.target === lightbox) {
                                closeLightbox();
                            }
                        });
                    }
                });

                function toggleToolResult(btn) {
                    const box = btn.closest('.tool-result-box');
                    if (!box) return;
                    const content = box.querySelector('.tool-result-content');
                    if (!content) return;
                    if (content.style.display === 'none') {
                        content.style.display = 'block';
                        btn.textContent = '收起';
                    } else {
                        content.style.display = 'none';
                        btn.textContent = '展开';
                    }
                    if (typeof _scrollToBottom === 'function') {
                        _scrollToBottom();
                    }
                }

                function showLightbox(src) {
                    const lightbox = document.getElementById('lightbox');
                    const img = document.getElementById('lightbox-img');
                    if (!lightbox || !img) return;
                    img.src = src;
                    if (rafId) {
                        cancelAnimationFrame(rafId);
                        rafId = null;
                    }
                    img.classList.remove('dragging');
                    lightboxScale = 1.0;
                    translateX = 0;
                    translateY = 0;
                    img.style.transform = 'translate(0px, 0px) scale(1)';
                    lightbox.style.display = 'flex';
                    lightbox.offsetHeight;
                    lightbox.classList.add('active');
                }
                function closeLightbox() {
                    const lightbox = document.getElementById('lightbox');
                    const img = document.getElementById('lightbox-img');
                    if (img) {
                        img.classList.remove('dragging');
                    }
                    if (!lightbox) return;
                    lightbox.classList.remove('active');
                    setTimeout(() => {
                        lightbox.style.display = 'none';
                    }, 200);
                }
                document.addEventListener('keydown', function(e) {
                    if (e.key === 'Escape') {
                        closeLightbox();
                    }
                });

function _renderMath(element) {
                    if (window._isStreaming) return;
                    if (typeof renderMathInElement === 'function') {
                        renderMathInElement(element || document.body, {
                            delimiters: KATEX_DELIMITERS,
                            throwOnError: false,
                            errorColor: 'transparent'
                        });
                    }
                    (element || document.body).querySelectorAll('.katex-error').forEach(function(el) {
                        if (el.closest('.math-fallback')) return;
                        var wrapper = document.createElement('code');
                        wrapper.className = 'math-fallback';
                        wrapper.textContent = el.textContent;
                        el.replaceWith(wrapper);
                    });
                }
                // ── DOM Windowing (按轮次, 1 轮 = 1 条 user 消息 + N 条 assistant 回复) ──
                const MAX_VISIBLE_ROUNDS = 10;
                const REVEAL_BATCH_ROUNDS = 3;
                let _showAllMessages = false;

                const SCROLL_THRESHOLD = 20;
                let _autoScroll = true;
                function _content() { return document.getElementById('content'); }
                window.addEventListener('load', function() {
                    var el = _content();
                    if (el) {
                        el.addEventListener('scroll', function() {
                            _autoScroll = (el.clientHeight + el.scrollTop >= el.scrollHeight - SCROLL_THRESHOLD);
                        });
                    }
                });
                let _scrollRafId = null;
                function _scrollToBottom() {
                    if (_autoScroll) {
                        if (_scrollRafId) {
                            cancelAnimationFrame(_scrollRafId);
                        }
                        _scrollRafId = requestAnimationFrame(() => {
                            var el = _content();
                            if (el) el.scrollTop = el.scrollHeight;
                            _scrollRafId = null;
                            // WebKit2GTK 程序化滚动可能不触发 scroll 事件，
                            // 主动刷新轮次/按钮状态
                            _updateRoundNav();
                        });
                    }
                }

                // ── DOM Windowing functions ──
                function applyWindowing() {
                    if (_showAllMessages) return;
                    var content = document.getElementById('content');
                    if (!content) return;
                    var allRows = content.querySelectorAll(':scope > .msg-row');
                    var userRows = content.querySelectorAll(':scope > .msg-row.user');
                    // 按轮次：每轮 = 一条 user 消息及其后的 AI 回复
                    if (userRows.length <= MAX_VISIBLE_ROUNDS) {
                        for (var i = 0; i < allRows.length; i++) {
                            allRows[i].classList.remove('msg-windowed');
                        }
                        updateShowOlderBar();
                        return;
                    }
                    // 找出倒数第 MAX_VISIBLE_ROUNDS 条 user 消息的 DOM 索引
                    var keepFromUser = userRows[userRows.length - MAX_VISIBLE_ROUNDS];
                    var keepFromIndex = -1;
                    for (var i = 0; i < allRows.length; i++) {
                        if (allRows[i] === keepFromUser) {
                            keepFromIndex = i;
                            break;
                        }
                    }
                    // 保留该 user 消息及之后的所有内容（含工具调用等）
                    for (var i = 0; i < keepFromIndex; i++) {
                        allRows[i].classList.add('msg-windowed');
                    }
                    for (var i = keepFromIndex; i < allRows.length; i++) {
                        allRows[i].classList.remove('msg-windowed');
                    }
                    updateShowOlderBar();
                }

                function showOlderBatch() {
                    var allRows = document.querySelectorAll('#content > .msg-row');
                    var userRows = document.querySelectorAll('#content > .msg-row.user');
                    // 找到第一个当前可见的 user 行
                    var firstVisibleUserIdx = -1;
                    for (var i = 0; i < userRows.length; i++) {
                        if (!userRows[i].classList.contains('msg-windowed')) {
                            firstVisibleUserIdx = i;
                            break;
                        }
                    }
                    if (firstVisibleUserIdx <= 0) return;
                    // 从隐藏区末尾往前揭示 REVEAL_BATCH_ROUNDS 轮
                    var revealCount = Math.min(firstVisibleUserIdx, REVEAL_BATCH_ROUNDS);
                    var newFirstUserIdx = firstVisibleUserIdx - revealCount;
                    var newFirstUser = userRows[newFirstUserIdx];
                    var firstVisibleUser = userRows[firstVisibleUserIdx];
                    var revealing = false;
                    for (var i = 0; i < allRows.length; i++) {
                        if (allRows[i] === newFirstUser || revealing) {
                            revealing = true;
                            allRows[i].classList.remove('msg-windowed');
                        }
                        if (allRows[i] === firstVisibleUser) break;
                    }
                    updateShowOlderBar();
                    _updateRoundNav();
                }

                function showAllMessages() {
                    _showAllMessages = true;
                    var hidden = document.querySelectorAll('#content > .msg-windowed');
                    for (var i = 0; i < hidden.length; i++) {
                        hidden[i].classList.remove('msg-windowed');
                    }
                    var bar = document.getElementById('show-older-bar');
                    if (bar) bar.style.display = 'none';
                    _updateRoundNav();
                }

                function updateShowOlderBar() {
                    var userRows = document.querySelectorAll('#content > .msg-row.user');
                    var hiddenRounds = 0;
                    for (var i = 0; i < userRows.length; i++) {
                        if (userRows[i].classList.contains('msg-windowed')) hiddenRounds++;
                    }
                    var bar = document.getElementById('show-older-bar');
                    var countSpan = document.getElementById('hidden-count');
                    if (!bar || !countSpan) return;
                    if (hiddenRounds > 0) {
                        countSpan.textContent = hiddenRounds;
                        bar.style.display = '';
                    } else {
                        bar.style.display = 'none';
                    }
                }

                function updateContent(html) {
                    window._isStreaming = false;
                    _showAllMessages = false;
                    resetReasoning();
                    // 内容替换（切换对话/重新渲染）后默认贴底：
                    // _autoScroll 是跨对话全局状态，若用户上次在顶部（如点过 ⤴）
                    // 会被置为 false，导致 _scrollToBottom 被门控跳过，新对话停在顶部
                    _autoScroll = true;
                    const content = document.getElementById('content');
                    const bar = document.getElementById('show-older-bar');
                    content.innerHTML = html;
                    // show-older-bar 位于 #content 内（随消息区滚动），
                    // innerHTML 重写后需重新挂载到首位
                    if (bar) content.insertBefore(bar, content.firstChild);
                    _wrapTables(content);
                    addCopyButtons();
                    _renderMath(content);
                    // 同步执行折叠（切换对话/内容替换后立即恢复"保留最近 10 轮"），
                    // 不依赖 RAF 防抖——防抖可能被流式/重建路径吞掉导致折叠失效
                    applyWindowing();
                    _scrollToBottom();
                    _initRoundNav();
                }
                /**
                 * _wrapTables - wrap every <table> in a .table-scroll div so wide
                 * tables scroll inside the bubble instead of overflowing the page
                 * (<table> ignores overflow-x, a block div honors it).
                 * Idempotent: already-wrapped tables are skipped.
                 */
                function _wrapTables(root) {
                    const scope = root || document;
                    scope.querySelectorAll('table').forEach(function(t) {
                        if (t.parentElement && t.parentElement.classList.contains('table-scroll')) return;
                        const wrap = document.createElement('div');
                        wrap.className = 'table-scroll';
                        t.parentNode.insertBefore(wrap, t);
                        wrap.appendChild(t);
                    });
                }
                function appendMessageContainer(msgId) {
                    window._isStreaming = true;
                    const content = document.getElementById('content');
                    if (!document.getElementById(msgId)) {
                        const row = document.createElement('div');
                        row.id = msgId;
                        row.className = 'msg-row assistant';
                        
                        const avatar = document.createElement('div');
                        avatar.className = 'msg-avatar assistant';
                        avatar.innerHTML = '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M12 2L14.8 9.2L22 12L14.8 14.8L12 22L9.2 14.8L2 12L9.2 9.2L12 2Z"/></svg>';
                        row.appendChild(avatar);
                        
                        const bubble = document.createElement('div');
                        bubble.className = 'msg-bubble assistant';
                        bubble.id = msgId + '-bubble';
                        // 三区结构：reasoning / tool / answer
                        bubble.innerHTML = ''
                            + '<div class="bubble-region reasoning-region"></div>'
                            + '<div class="bubble-region tool-region"></div>'
                            + '<div class="bubble-region answer-region">'
                            +   '<div class="typing-indicator"><div class="typing-dot"></div><div class="typing-dot"></div><div class="typing-dot"></div></div>'
                            + '</div>'
                            + '<copy-marker></copy-marker>';
                        row.appendChild(bubble);
                        
                        content.appendChild(row);
                    }
                    // ── Streaming v2: 记录当前流式容器的 ID ──
                    _streamingContainerId = msgId;
                    _streamingTextNode = null;
                    _throttledWindowing();
                    _scrollToBottom();
                }
                function updateMessageContainer(msgId, html, isSplit) {
                    const container = document.getElementById(msgId);
                    if (!container) return;
                    if (isSplit) {
                        container.className = ''; // Remove container styling for split layout
                        container.innerHTML = html;
                        _wrapTables(container);
                        addCopyButtons();
                        _debouncedRenderMath(container);
                    } else {
                        const div = document.getElementById(msgId + '-bubble') || container;
                        var regions = div.querySelectorAll('.bubble-region');
                        if (regions.length === 3) {
                            // 三区结构：分别更新各区域，保留未提供的区域不变
                            var temp = document.createElement('div');
                            temp.innerHTML = html;
                            var reasoning = temp.querySelector('.reasoning-region');
                            var tools = temp.querySelector('.tool-region');
                            var answer = temp.querySelector('.answer-region');
                            if (reasoning && regions[0]) {
                                // 如果 Python 发送了空 reasoning HTML，不覆盖 JS 管理的 thinking badge
                                if (reasoning.innerHTML.trim()) {
                                    regions[0].innerHTML = reasoning.innerHTML;
                                }
                            }
                            if (tools && regions[1]) regions[1].innerHTML = tools.innerHTML;
                            if (answer && regions[2]) {
                                // 移除 typing-indicator（如果存在）
                                var typing = regions[2].querySelector('.typing-indicator');
                                if (typing) typing.remove();
                                regions[2].innerHTML = answer.innerHTML;
                            }
                            addCopyButtons();
                            _wrapTables(div);   // 覆盖全部三个区域（幂等），与 isSplit/旧结构分支一致
                            _debouncedRenderMath(div);
                        } else {
                            // 旧结构：向后兼容
                            div.innerHTML = html;
                            _wrapTables(div);
                            addCopyButtons();
                            _debouncedRenderMath(div);
                        }
                    }
                    _throttledWindowing();
                    _scrollToBottom();
                }
                function addCopyButtons() {
                    document.querySelectorAll('pre:not(.has-copy-btn)').forEach(function(pre) {
                        if (pre.classList.contains('tool-result-content')) return;
                        
                        const code = pre.querySelector('code');
                        if (code) {
                            let lang = 'CODE';
                            code.classList.forEach(function(cls) {
                                if (cls.startsWith('language-')) {
                                    lang = cls.replace('language-', '').toUpperCase();
                                }
                            });
                            pre.setAttribute('data-lang', lang);
                        }


                        const btn = document.createElement('button');
                        btn.className = 'copy-btn';
                        btn.textContent = '复制';
                        btn.addEventListener('click', function() {
                            const code = pre.querySelector('code');
                            const text = code ? code.textContent : pre.textContent;
                            if (navigator.clipboard && navigator.clipboard.writeText) {
                                navigator.clipboard.writeText(text).then(function() {
                                    btn.textContent = '✓';
                                    btn.classList.add('copied');
                                    setTimeout(function() { btn.textContent = '复制'; btn.classList.remove('copied'); }, 2000);
                                }).catch(function(e) {
                                    console.warn('Copy failed, trying fallback:', e);
                                    fallbackCopy(text, function() {
                                        btn.textContent = '✓';
                                        btn.classList.add('copied');
                                        setTimeout(function() { btn.textContent = '复制'; btn.classList.remove('copied'); }, 2000);
                                    });
                                });
                            } else {
                                fallbackCopy(text, function() {
                                    btn.textContent = '✓';
                                    btn.classList.add('copied');
                                    setTimeout(function() { btn.textContent = '复制'; btn.classList.remove('copied'); }, 2000);
                                });
                            }
                        });
                        pre.appendChild(btn);
                        pre.classList.add('has-copy-btn');
                    });
                    function fallbackCopy(text, done) {
                        const ta = document.createElement('textarea');
                        ta.value = text;
                        document.body.appendChild(ta);
                        ta.select();
                        document.execCommand('copy');
                        document.body.removeChild(ta);
                        done();
                    }
                    addMessageCopyButtons();
                    addRetryButtons();
                    addUserMessageCopyButtons();
                }
                function _addCopyButtonsForMarkers(selector, btnText, uriPrefix, idxPrefix) {
                    document.querySelectorAll(selector).forEach(function(marker) {
                        var idx = marker.dataset.msgIndex;
                        var dataIdx = idxPrefix + idx;
                        if (marker.parentNode?.querySelector('.msg-btn-row[data-idx="' + dataIdx + '"]')) return;
                        var row = document.createElement('div');
                        row.className = 'msg-btn-row';
                        row.setAttribute('data-idx', dataIdx);
                        const btn = document.createElement('button');
                        btn.className = 'msg-copy-btn' + (idxPrefix ? ' msg-copy-user-btn' : '');
                        btn.textContent = btnText;
                        btn.addEventListener('click', function(e) {
                            e.stopPropagation();
                            window.location = uriPrefix + '?index=' + idx;
                        });
                        row.appendChild(btn);
                        marker.parentNode.insertBefore(row, marker);
                    });
                }
                function addMessageCopyButtons() {
                    _addCopyButtonsForMarkers('copy-marker:not(.user-copy-marker)', '📋 复制回答', 'opencode://copy-response', '');
                }
                function addRetryButtons() {
                    var markers = document.querySelectorAll('copy-marker:not(.user-copy-marker)');
                    var lastIdx = -1;
                    markers.forEach(function(m) {
                        var idx = parseInt(m.dataset.msgIndex);
                        if (!isNaN(idx) && idx > lastIdx) lastIdx = idx;
                    });
                    if (lastIdx < 0) return;
                    var row = document.querySelector('.msg-btn-row[data-idx="' + lastIdx + '"]');
                    if (!row || row.querySelector('.retry-btn')) return;
                    var btn = document.createElement('button');
                    btn.className = 'retry-btn';
                    btn.textContent = '🔄 重新生成';
                    btn.addEventListener('click', function(e) {
                        e.stopPropagation();
                        window.location = 'opencode://retry?index=' + lastIdx;
                    });
                    row.appendChild(btn);
                }
                function addUserMessageCopyButtons() {
                    _addCopyButtonsForMarkers('copy-marker.user-copy-marker', '📋 复制输入', 'opencode://copy-input', 'u-');
                }

                /* ── Round Navigation ─────────────────────── */
                var _currentRound = 1;
                var _roundNavInitialized = false;
                var _rafId = null;
                function _initRoundNav() {
                    if (!_roundNavInitialized) {
                        var el = _content();
                        if (el) {
                            el.addEventListener('scroll', function() {
                                if (_rafId) return;
                                _rafId = requestAnimationFrame(function() {
                                    _rafId = null;
                                    _updateRoundNav();
                                });
                            });
                        }
                        _roundNavInitialized = true;
                    }
                    _updateRoundNav();
                    var nav = document.getElementById('round-nav');
                    if (nav) nav.style.opacity = '0.5';
                }
                function _updateRoundNav() {
                    var userRows = document.querySelectorAll('.msg-row.user:not(.msg-windowed)');
                    var nav = document.getElementById('round-nav');
                    if (!nav) return;
                    var total = userRows.length;
                    if (total <= 1) { nav.style.display = 'none'; return; }
                    nav.style.display = 'flex';
                    var el = _content();
                    var scrollTop = el ? el.scrollTop : 0;
                    // content 在视口中的偏移（header 高度等），用于换算文档坐标：
                    // 元素在 content 文档中的位置 = rect.top - contentRect.top + scrollTop
                    var contentTop = el ? el.getBoundingClientRect().top : 0;
                    var found = 1;
                    var minDist = Infinity;
                    userRows.forEach(function(row, idx) {
                        var rect = row.getBoundingClientRect();
                        var rowTop = rect.top - contentTop + scrollTop;
                        var dist = Math.abs(rowTop - scrollTop);
                        if (dist < minDist) { minDist = dist; found = idx + 1; }
                    });
                    _currentRound = Math.max(1, Math.min(found, total));
                    var indicator = document.getElementById('round-indicator');
                    if (indicator) indicator.textContent = _currentRound + '/' + total;
                    var prevBtn = document.getElementById('round-prev');
                    var nextBtn = document.getElementById('round-next');
                    if (prevBtn) prevBtn.disabled = _currentRound <= 1;
                    if (nextBtn) nextBtn.disabled = _currentRound >= total;
                }
                function _scrollToRound(n) {
                    var userRows = document.querySelectorAll('.msg-row.user:not(.msg-windowed)');
                    if (n < 1 || n > userRows.length) return;
                    var target = userRows[n - 1];
                    if (target) {
                        var el = _content();
                        if (el) {
                            // 用 content 文档坐标（减去 content 视口偏移），
                            // 否则 header 高度会导致跳转位置整体偏移
                            var contentTop = el.getBoundingClientRect().top;
                            var top = target.getBoundingClientRect().top - contentTop + el.scrollTop - 10;
                            // 用 scrollTop 赋值而非 scrollTo(options)：
                            // 部分 WebKit2GTK 版本对 options 语法静默失败导致导航失效
                            el.scrollTop = top;
                            // 滚动后主动刷新轮次/按钮状态（不依赖 scroll 事件）
                            _updateRoundNav();
                        }
                    }
                }
                function _prevRound() { _updateRoundNav(); _scrollToRound(_currentRound - 1); }
                function _nextRound() { _updateRoundNav(); _scrollToRound(_currentRound + 1); }
                function _scrollToBottomForce() {
                    var el = _content();
                    if (el) {
                        void el.offsetHeight;
                        el.scrollTop = el.scrollHeight;
                    }
                    // 滚动后主动刷新轮次/按钮状态（不依赖 scroll 事件）
                    _updateRoundNav();
                }
                function _scrollToTopForce() {
                    var el = _content();
                    if (el) el.scrollTop = 0;
                    _updateRoundNav();
                }

                /**
                 * appendStreamToken - 增量追加流式文本到当前助手消息的 answer 区域。
                 * 在流式活跃期，只追加纯文本节点，不触发 HTML 解析或 KaTeX 渲染。
                 * 流结束时由 updateMessageContainer() 替换为最终渲染的 HTML。
                 */
                function appendStreamToken(text) {
                    if (!text) return;

                    const container = document.getElementById(_streamingContainerId);
                    if (!container) return;

                    const answerRegion = container.querySelector('.bubble-region.answer-region');
                    if (!answerRegion) return;

                    if (!_streamingTextNode) {
                        _streamingTextNode = document.createTextNode(text);
                        const typing = answerRegion.querySelector('.typing-indicator');
                        if (typing) typing.remove();
                        answerRegion.appendChild(_streamingTextNode);
                    } else {
                        _streamingTextNode.appendData(text);
                    }

                    _scrollToBottom();
                }

                /**
                 * appendStreamReasoning - 缓存推理文本，管理 thinking badge。
                 *
                 * 不再实时追加到 DOM，仅缓存文本。首次调用时启动 thinking badge。
                 * 思考完成后调用 finishReasoning() 切换为 thought badge（可展开）。
                 * 用户点击展开时从缓存懒渲染具体内容。
                 */
                function appendStreamReasoning(text) {
                    if (!text) return;

                    // 累积文本到 pending（后续由 _flushReasoningCache 刷入 cache）
                    _reasoningPendingText += text;

                    // 仅在首次（idle → thinking）启动 thinking badge
                    // 工具调用后（complete 状态）的 reasoning 只缓存，不再切换 badge
                    if (_reasoningState === 'idle') {
                        _startReasoning();
                    }
                }

                function _flushReasoningCache() {
                    if (!_reasoningPendingText) return;
                    _reasoningCache += _reasoningPendingText;
                    _reasoningPendingText = '';
                }

                /**
                 * _appendReasoningCacheOnly - 仅追加到缓存，不操作 DOM。
                 * 由 _finalize_streaming_render 在流结束时调用，避免触发 _startReasoning。
                 */
                function _appendReasoningCacheOnly(text) {
                    if (!text) return;
                    // 先 flush pending（来自 appendStreamReasoning 但尚未入 cache 的文本）
                    _flushReasoningCache();
                    _reasoningCache += text;
                }

                function _startReasoning() {
                    if (_reasoningState === 'thinking') {
                        // 已在 thinking 状态，只缓存文本
                        _flushReasoningCache();
                        return;
                    }
                    _reasoningState = 'thinking';
                    _flushReasoningCache();

                    const container = document.getElementById(_streamingContainerId);
                    if (!container) return;
                    const reasoningRegion = container.querySelector('.bubble-region.reasoning-region');
                    if (!reasoningRegion) return;

                    // 显示 thinking badge（不可展开，无计时器）
                    reasoningRegion.innerHTML = ''
                        + '<div class="reasoning-badge thinking" data-state="thinking">'
                        +   '<span class="reasoning-icon">💭</span>'
                        +   '<span class="reasoning-label">Thinking</span>'
                        + '</div>';

                    _scrollToBottom();
                }

                /**
                 * finishReasoning - 切换为 thought badge（可点击展开）。
                 *
                 * 可被多次调用（工具调用时、流结束时），幂等。
                 * 无计时器，仅显示 "Thought" 标签。
                 */
                function finishReasoning() {
                    if (_reasoningState === 'idle') return; // 从头到尾没有 reasoning

                    _reasoningState = 'complete';

                    // 刷新缓存
                    _flushReasoningCache();

                    try {
                        const container = document.getElementById(_streamingContainerId);
                        if (container) {
                            const badge = container.querySelector('.reasoning-badge');
                            if (badge && badge.classList.contains('thinking')) {
                                var escapedContent = (_reasoningCache || '')
                                    .replace(/&/g, '&amp;')
                                    .replace(/"/g, '&quot;')
                                    .replace(/</g, '&lt;')
                                    .replace(/>/g, '&gt;');
                                const region = badge.closest('.bubble-region.reasoning-region');
                                if (region) {
                                    region.innerHTML = ''
                                        + '<div class="reasoning-badge complete" onclick="toggleReasoning(this)"'
                                        + ' data-content="' + escapedContent + '">'
                                        +   '<span class="reasoning-icon">💭</span>'
                                        +   '<span class="reasoning-label">Thought</span>'
                                        +   '<span class="reasoning-expand-icon">▶</span>'
                                        + '</div>'
                                        + '<div class="reasoning-content" style="display:none;"></div>';
                                }
                            }
                        }
                        _scrollToBottom();
                    } finally {
                        _reasoningCache = '';
                        _reasoningPendingText = '';
                    }
                }

                /**
                 * toggleReasoning - 展开/收起思考内容（懒渲染）。
                 *
                 * 用户点击 thought badge 时触发。
                 * 首次展开时从缓存渲染内容，后续切换 display。
                 */
                function toggleReasoning(badgeEl) {
                    const region = badgeEl.closest('.bubble-region.reasoning-region');
                    if (!region) return;

                    const expandIcon = badgeEl.querySelector('.reasoning-expand-icon');
                    const contentDiv = region.querySelector('.reasoning-content');
                    if (!contentDiv) return;

                    if (contentDiv.style.display === 'none') {
                        // 展开
                        if (!contentDiv.dataset.rendered) {
                            _flushReasoningCache();
                            // 优先级：流式缓存 > 收起时保存的副本 > DOM 已有内容（服务端预渲染或旧缓存）
                            var text = _reasoningCache || badgeEl.dataset.content || contentDiv.textContent;
                            contentDiv.textContent = text;
                            contentDiv.dataset.rendered = 'true';
                        }
                        contentDiv.style.display = 'block';
                        if (expandIcon) expandIcon.textContent = '▼';
                    } else {
                        // 收起——从 DOM 中彻底移除内容，下次展开重新渲染
                        var currentText = contentDiv.textContent;
                        if (currentText) {
                            badgeEl.dataset.content = currentText;  // 保存副本用于恢复
                        }
                        contentDiv.textContent = '';
                        delete contentDiv.dataset.rendered;
                        contentDiv.style.display = 'none';
                        if (expandIcon) expandIcon.textContent = '▶';
                    }

                    _scrollToBottom();
                }

                /**
                 * resetReasoning - 重置 reasoning 状态（新对话时调用）。
                 */
                function resetReasoning() {
                    _reasoningState = 'idle';
                    _reasoningCache = '';
                    _reasoningPendingText = '';
                }

                /**
                 * removeTypingIndicators - 移除 DOM 中所有闪烁点，终止流状态。
                 */
                function removeTypingIndicators() {
                    document.querySelectorAll('.typing-indicator').forEach(function (el) {
                        el.remove();
                    });
                    window._isStreaming = false;
                }

                /**
                 * updateToolCard - 增量更新工具卡片的内容。
                 * 在工具结果到达时调用，只更新指定卡片，不触发全量渲染。
                 */
                function updateToolCard(toolCallId, cardHtml) {
                    if (!toolCallId || !cardHtml) return;

                    const details = document.querySelector('[data-tool-call-id="' + toolCallId + '"]');
                    if (!details) return;

                    details.outerHTML = cardHtml;

                    const newDetails = document.querySelector('[data-tool-call-id="' + toolCallId + '"]');
                    if (newDetails) {
                        _debouncedRenderMath(newDetails);
                        addCopyButtons();
                        _wrapTables(newDetails);  // 工具卡片内表格同样包裹成横向滚动
                    }

                    _scrollToBottom();
                }

// ── 初始表格包裹 ──
// load_html 将 initial_html（可能含宽表格）直接嵌入 #content，页面刚加载时
// 无 updateContent 调用，嵌入的表格不会被 _wrapTables 包裹（表现为固定宽度
// + 内容强制换行而非横向滚动）。此处 DOM 就绪后主动包裹一次；此后
// updateContent / updateMessageContainer 会再次调用（幂等）。
document.addEventListener('DOMContentLoaded', function () {
    const content = document.getElementById('content');
    if (content) _wrapTables(content);  // 判空：防止模板结构变化时退化为全文档扫描
});

_scrollToBottom();
                _initRoundNav();

// ── 历史对话空态文案 ────────────────────────────────────────────────────────
const _EMPTY_NO_CONV = "（暂无历史对话）";
const _EMPTY_NO_MATCH = "没有匹配的对话";

/**
 * _formatRelativeTime - 将毫秒时间戳格式化为相对时间文本。
 * 纯函数，不触碰 DOM。ts 字段在 Python 侧为毫秒整数（Date.now() 同单位）。
 * @param {number} tsMs 毫秒时间戳
 * @returns {string} 相对时间描述或 YYYY-MM-DD 日期
 */
function _formatRelativeTime(tsMs) {
    var now = Date.now();
    var diff = now - tsMs;
    var minute = 60 * 1000;
    var hour = 60 * minute;
    var day = 24 * hour;

    if (diff < 60 * 1000) return "刚刚";
    if (diff < hour) return Math.floor(diff / minute) + "分钟前";
    if (diff < day) return Math.floor(diff / hour) + "小时前";
    if (diff < 2 * day) return "昨天";
    if (diff < 7 * day) return Math.floor(diff / day) + "天前";

    var d = new Date(tsMs);
    function pad(n) {
        return n < 10 ? "0" + n : "" + n;
    }
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
}

// ── AI Header（WebView 内固定装饰区）交互 ───────────────────────────────────

function showHeaderSpinner() {
    var el = document.getElementById('ai-header-spinner');
    if (el) el.style.display = '';
}
function hideHeaderSpinner() {
    var el = document.getElementById('ai-header-spinner');
    if (el) el.style.display = 'none';
}
function updateHeaderTitle(titleHtml, modelText) {
    var t = document.getElementById('ai-header-title');
    if (t && titleHtml) t.innerHTML = titleHtml;
    var m = document.getElementById('ai-header-model');
    if (m) m.textContent = modelText || '';
}
function updateHistoryLabel(label) {
    var btn = document.getElementById('ai-history-btn');
    if (btn) {
        while (btn.firstChild) btn.removeChild(btn.firstChild);
        btn.appendChild(document.createTextNode(label + ' ▾'));
    }
}
function closeAIPanel() {
    window.location = 'opencode://close-panel';
}
function toggleHistoryDropdown() {
    var dd = document.getElementById('ai-history-dropdown');
    if (!dd) return;
    if (dd.style.display === 'none') {
        // 收起态 → 展开：请求数据（renderHistoryList 内部会显示面板）
        window.location = 'opencode://history-open';
    } else {
        // 展开态 → 收起（display 为 '' 等非 none 值均视为展开）
        dd.style.display = 'none';
    }
}
// 点击下拉外部区域时收起（对齐原 GTK Popover 行为）
document.addEventListener('click', function (ev) {
    var dd = document.getElementById('ai-history-dropdown');
    var btn = document.getElementById('ai-history-btn');
    if (!dd || dd.style.display === 'none') return;
    if (!dd.contains(ev.target) && (!btn || !btn.contains(ev.target))) {
        dd.style.display = 'none';
        // 点击外部关闭：通知 Python 侧同步收起态（T9 接收端）
        window.location = 'opencode://history-close';
    }
});
function hideHistoryDropdown() {
    var dd = document.getElementById('ai-history-dropdown');
    if (dd) dd.style.display = 'none';
}
// ── 历史对话搜索过滤 ────────────────────────────────────────────────────────
// 缓存最近一次解析的 items / currentId，供搜索框输入重渲时复用
// （refresh_dropdown 每次全量推送，此处仅作前端过滤缓存，不持有额外状态）
var _historyItems = null;       // 最近一次解析的 items 数组
var _historyCurrentId = null;   // 最近一次渲染的 currentId
var _historyHlIndex = -1;       // 键盘导航高亮行索引（-1 = 无高亮）
var _suppressRowAnim = false;   // 搜索/重渲期间抑制行入场动画（避免批量闪动）

/**
 * _filterItems - 纯函数：按 label 子串过滤（大小写不敏感）。
 * 空查询或无匹配时返回原数组。
 * @param {Array} items items 数组（每项含 id/label/ts）
 * @param {string} q 搜索词（可能为空串）
 * @returns {Array} 过滤后的 items
 */
function _filterItems(items, q) {
    if (!q) return items;
    var lower = q.toLowerCase();
    return items.filter(function (it) {
        return it.label && it.label.toLowerCase().indexOf(lower) !== -1;
    });
}

/**
 * _renderHistoryEmpty - 渲染单行空态（无对话 / 无匹配共用）。
 * 不使用 innerHTML，全部 createElement + textContent（XSS 安全）。
 */
function _renderHistoryEmpty(text) {
    var list = document.getElementById('ai-history-list');
    if (!list) return;
    list.innerHTML = '';
    var empty = document.createElement('div');
    empty.className = 'ai-history-row';
    empty.textContent = text;
    list.appendChild(empty);
}

/**
 * _renderHistoryRows - 构建历史对话行列表（全量 / 过滤结果共用）。
 * 编辑模式下按 _historySelected 恢复 h-sel 状态（过滤重渲后保留选中）。
 */
function _renderHistoryRows(items, currentId) {
    var list = document.getElementById('ai-history-list');
    if (!list) return;
    list.innerHTML = '';
    if (!items || !items.length) return;  // 空态由调用方 renderHistoryList 区分渲染
    items.forEach(function (it, i) {
        var row = document.createElement('div');
        row.className = 'ai-history-row' + (it.id === currentId ? ' active' : '');
        row.dataset.id = it.id;
        // 行入场动画：仅非抑制时加 .row-enter + 错峰延迟（最多 10 行，避免大量行动画卡顿）
        if (!_suppressRowAnim) {
            row.classList.add('row-enter');
            row.style.animationDelay = Math.min(i, 9) * 30 + 'ms';
        }
        if (_historyEditMode && _historySelected[it.id]) row.classList.add('h-sel');
        var title = document.createElement('span');
        title.textContent = it.label;
        row.appendChild(title);
        var time = document.createElement('span');
        time.className = 'ai-history-time';
        time.textContent = it.ts ? _formatRelativeTime(it.ts) : '';
        row.appendChild(time);
        var del = document.createElement('button');
        del.className = 'h-del';
        del.textContent = '✕';
        del.title = '删除该对话';
        del.onclick = function (ev) {
            ev.stopPropagation();
            var id = row.dataset.id;
            askConfirm('删除该对话？', function () {
                window.location = 'opencode://history-delete?id=' + encodeURIComponent(id);
            });
        };
        row.appendChild(del);
        row.onclick = function () {
            if (_historyEditMode) {
                row.classList.toggle('h-sel');
                if (row.classList.contains('h-sel')) _historySelected[row.dataset.id] = true;
                else delete _historySelected[row.dataset.id];
                updateDeleteSelLabel();
            } else {
                window.location = 'opencode://history-select?id=' + encodeURIComponent(row.dataset.id);
            }
        };
        // 编辑模式下隐藏单行删除按钮
        if (_historyEditMode) del.style.display = 'none';
        list.appendChild(row);
    });
}

function renderHistoryList(itemsJson, currentId, show) {
    var list = document.getElementById('ai-history-list');
    var dd = document.getElementById('ai-history-dropdown');
    if (!list || !dd) return;
    var items = (typeof itemsJson === 'string') ? JSON.parse(itemsJson) : itemsJson;
    if (!items || !items.length) items = [];
    _historyItems = items;
    _historyCurrentId = currentId;
    // 搜索激活期间（refresh_dropdown 等重渲）：从搜索框读回搜索词并重放过滤
    var q = '';
    var searchInput = document.getElementById('ai-history-search');
    if (searchInput) q = searchInput.value;
    var visible = _filterItems(items, q);
    if (!items.length) {
        _renderHistoryEmpty(_EMPTY_NO_CONV);
    } else if (!visible.length) {
        _renderHistoryEmpty(_EMPTY_NO_MATCH);
    } else {
        // 下拉不可见时的重渲不触发行动画（refresh_dropdown 等后台刷新场景）
        if (dd.style.display === 'none') {
            _suppressRowAnim = true;
            setTimeout(function () {
                _suppressRowAnim = false;
            }, 60);
        }
        _renderHistoryRows(visible, currentId);
    }
    // 键盘导航：过滤重渲后 clamp 高亮索引并恢复 .hl（行数变少时越界落到末尾）
    _historyClampHl();
    // 仅当显式要求显示时才展开（refresh_dropdown 等数据刷新场景保持收起）
    if (show) dd.style.display = '';
}
function showHistoryDropdown() {
    var dd = document.getElementById('ai-history-dropdown');
    if (dd) {
        dd.style.display = '';
        // 打开动画：先清 closing 态并强制 reflow，保证 ddFadeIn 从初始态播放
        dd.classList.remove('dd-closing');
        void dd.offsetWidth;
        dd.classList.add('dd-open');
    }
    // 展开时重置键盘高亮并聚焦搜索框（打开后即可用 ↑↓ 导航）
    _historyHlIndex = -1;
    document.querySelectorAll('#ai-history-list .ai-history-row.hl').forEach(function (r) {
        r.classList.remove('hl');
    });
    var si = document.getElementById('ai-history-search');
    if (si) si.focus();
}

// ── 历史对话键盘导航 ────────────────────────────────────────────────────────
// 搜索框聚焦时 ↑↓/Enter/Esc/Home/End 操作下拉列表；高亮复用 T8 提供的 .hl 样式

/**
 * _historyRows - 收集当前可见的真实行（跳过空态占位行）。
 */
function _historyRows() {
    var rows = [];
    document.querySelectorAll('#ai-history-list .ai-history-row').forEach(function (r) {
        if (r.dataset.id) rows.push(r);
    });
    return rows;
}

/**
 * _historySetHl - 设置高亮行（index 越界时 clamp），并按需滚动到可见。
 * 滚动容器为 #ai-history-list（三区域布局下独立滚动容器，overflow-y: auto）。
 * 滚动沿用 scrollTop 手动对齐（_scrollToRound 注释：部分 WebKit2GTK
 * 版本对 scrollTo(options) 静默失败），实现 block:'nearest' 语义。
 */
function _historySetHl(index, scroll) {
    var rows = _historyRows();
    if (!rows.length) {
        _historyHlIndex = -1;
        return;
    }
    if (index < 0) index = 0;
    if (index >= rows.length) index = rows.length - 1;
    _historyHlIndex = index;
    rows.forEach(function (r, i) {
        r.classList.toggle('hl', i === _historyHlIndex);
    });
    if (scroll) {
        var el = document.getElementById('ai-history-list');
        var target = rows[_historyHlIndex];
        if (el && target) {
            var listTop = el.getBoundingClientRect().top;
            var top = target.getBoundingClientRect().top - listTop + el.scrollTop;
            var bottom = top + (target.offsetHeight || 0);
            if (top < el.scrollTop) {
                el.scrollTop = top;
            } else if (bottom > el.scrollTop + el.clientHeight) {
                el.scrollTop = bottom - el.clientHeight;
            }
        }
    }
}

/**
 * _historyClampHl - 列表重渲后调用：高亮索引越界时 clamp 并恢复 .hl。
 * -1（无高亮）态保持不动，避免首开时误高亮末行。
 */
function _historyClampHl() {
    var rows = _historyRows();
    if (!rows.length) {
        _historyHlIndex = -1;
        return;
    }
    if (_historyHlIndex < 0) return;
    if (_historyHlIndex >= rows.length) _historyHlIndex = rows.length - 1;
    _historySetHl(_historyHlIndex, false);
}

// ── 历史对话搜索框绑定（DOM 就绪后；脚本内联于 <head>，元素此时才存在） ──
document.addEventListener('DOMContentLoaded', function () {
    var input = document.getElementById('ai-history-search');
    if (!input) return;
    // 输入即过滤：按 label 子串过滤并重渲列表（仅下拉展开时可输入）
    input.addEventListener('input', function () {
        // 过滤重渲期间抑制行入场动画，60ms 后恢复（不影响后续打开动画）
        _suppressRowAnim = true;
        renderHistoryList(_historyItems || [], _historyCurrentId);
        setTimeout(function () {
            _suppressRowAnim = false;
        }, 60);
    });
    // IME 守卫：中文拼音等组合输入期间 keydown 以 keyCode 229 上报，
    // 提前 return 跳过按键逻辑（下方 ↑↓/Enter/Esc/Home/End 均不处理组合输入）
    input.addEventListener('keydown', function (e) {
        if (e.isComposing === true || e.keyCode === 229) return;
        var rows = _historyRows();
        var key = e.key;
        if (key === 'ArrowDown' || key === 'ArrowUp') {
            // 移动高亮：越界环绕（到底回 0，到顶回最后）
            e.preventDefault();
            if (!rows.length) return;
            var next = _historyHlIndex + (key === 'ArrowDown' ? 1 : -1);
            if (next >= rows.length) next = 0;
            if (next < 0) next = rows.length - 1;
            _historySetHl(next, true);
        } else if (key === 'Home' || key === 'End') {
            e.preventDefault();
            if (!rows.length) return;
            _historySetHl(key === 'Home' ? 0 : rows.length - 1, true);
        } else if (key === 'Enter') {
            e.preventDefault();  // 防与行 click 双重触发
            if (_historyHlIndex < 0 || _historyHlIndex >= rows.length) return;  // 无高亮忽略
            var row = rows[_historyHlIndex];
            if (row) {
                if (typeof row.onclick === 'function') {
                    row.onclick();  // 编辑模式 toggle h-sel / 非编辑发 history-select
                } else {
                    // 兜底：行无 onclick 时直接执行选择逻辑（切到编辑模式检查）
                    if (_historyEditMode) {
                        row.classList.toggle('h-sel');
                        if (row.classList.contains('h-sel')) _historySelected[row.dataset.id] = true;
                        else delete _historySelected[row.dataset.id];
                        updateDeleteSelLabel();
                    } else {
                        window.location = 'opencode://history-select?id=' + encodeURIComponent(row.dataset.id);
                    }
                }
            }
        } else if (key === 'Escape') {
            e.preventDefault();
            hideHistoryDropdown();
            window.location = 'opencode://history-close';
        }
    });
});
// ── 二次确认条 ──
var _confirmAction = null;
function askConfirm(msg, fn) {
    _confirmAction = fn;
    var m = document.getElementById('history-confirm-msg');
    if (m) m.textContent = msg;
    var bar = document.getElementById('history-confirm-bar');
    if (bar) bar.style.display = '';
}
function confirmOk() {
    if (_confirmAction) {
        var fn = _confirmAction;
        _confirmAction = null;
        fn();
    }
    hideConfirm();
}
function confirmCancel() {
    _confirmAction = null;
    hideConfirm();
}
function hideConfirm() {
    var bar = document.getElementById('history-confirm-bar');
    if (bar) bar.style.display = 'none';
}
// ── 编辑模式（多选删除） ──
var _historyEditMode = false;
var _historySelected = {};
function toggleHistoryEditMode() {
    _historyEditMode = !_historyEditMode;
    _historySelected = {};
    var editBtn = document.getElementById('history-edit-btn');
    var selAllBtn = document.getElementById('history-select-all-btn');
    var delSelBtn = document.getElementById('history-delete-sel-btn');
    if (editBtn) editBtn.textContent = _historyEditMode ? '完成' : '编辑';
    if (selAllBtn) selAllBtn.style.display = _historyEditMode ? '' : 'none';
    if (delSelBtn) delSelBtn.style.display = _historyEditMode ? '' : 'none';
    var rows = document.querySelectorAll('#ai-history-list .ai-history-row');
    rows.forEach(function (row) {
        row.classList.remove('h-sel');
        var del = row.querySelector('.h-del');
        if (del) del.style.display = _historyEditMode ? 'none' : '';
    });
    hideConfirm();
    updateDeleteSelLabel();
}
function historySelectAll() {
    // 仅遍历当前可见（过滤后）的真实行：跳过空态占位行（无 dataset.id）
    var rows = [];
    document.querySelectorAll('#ai-history-list .ai-history-row').forEach(function (r) {
        if (r.dataset.id) rows.push(r);
    });
    var allSelected = rows.length > 0;
    rows.forEach(function (r) { if (!r.classList.contains('h-sel')) allSelected = false; });
    rows.forEach(function (r) {
        r.classList.toggle('h-sel', !allSelected);
        if (!allSelected) _historySelected[r.dataset.id] = true;
    });
    if (allSelected) _historySelected = {};
    updateDeleteSelLabel();
}
function updateDeleteSelLabel() {
    var btn = document.getElementById('history-delete-sel-btn');
    if (btn) btn.textContent = '删除选中(' + Object.keys(_historySelected).length + ')';
}
function historyDeleteSelected() {
    var ids = Object.keys(_historySelected);
    if (!ids.length) return;
    askConfirm('删除选中的 ' + ids.length + ' 个对话？', function () {
        toggleHistoryEditMode();  // 先退出编辑模式，刷新后列表为普通态
        window.location = 'opencode://history-delete-multi?ids=' + encodeURIComponent(ids.join(','));
    });
}
function historyAction(kind) {
    if (kind === 'clear') {
        askConfirm('清空已删除的对话？', function () {
            window.location = 'opencode://history-clear';
        });
    } else if (kind === 'edit') {
        toggleHistoryEditMode();
    }
}
