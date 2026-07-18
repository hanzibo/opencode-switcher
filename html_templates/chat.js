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
                            currentX = e.clientX - startX;
                            currentY = e.clientY - startY;
                            dragDistance += Math.abs(currentX - translateX) + Math.abs(currentY - translateY);
                            
                            if (!rafId) {
                                rafId = requestAnimationFrame(updateTransform);
                            }
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
                window.addEventListener('scroll', function() {
                    _autoScroll = (window.innerHeight + window.scrollY >= document.body.scrollHeight - SCROLL_THRESHOLD);
                });
                let _scrollRafId = null;
                function _scrollToBottom() {
                    if (_autoScroll) {
                        if (_scrollRafId) {
                            cancelAnimationFrame(_scrollRafId);
                        }
                        _scrollRafId = requestAnimationFrame(() => {
                            window.scrollTo(0, document.body.scrollHeight);
                            _scrollRafId = null;
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
                    const content = document.getElementById('content');
                    content.innerHTML = html;
                    addCopyButtons();
                    _renderMath(content);
                    _throttledWindowing();
                    _scrollToBottom();
                    _initRoundNav();
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
                            _debouncedRenderMath(div);
                        } else {
                            // 旧结构：向后兼容
                            div.innerHTML = html;
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
                        window.addEventListener('scroll', function() {
                            if (_rafId) return;
                            _rafId = requestAnimationFrame(function() {
                                _rafId = null;
                                _updateRoundNav();
                            });
                        });
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
                    var scrollTop = window.scrollY;
                    var found = 1;
                    var minDist = Infinity;
                    userRows.forEach(function(row, idx) {
                        var rect = row.getBoundingClientRect();
                        var rowTop = rect.top + window.scrollY;
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
                        var top = target.getBoundingClientRect().top + window.scrollY - 10;
                        window.scrollTo({top: top});
                    }
                }
                function _prevRound() { _scrollToRound(_currentRound - 1); }
                function _nextRound() { _scrollToRound(_currentRound + 1); }
                function _scrollToBottomForce() {
                    void document.body.offsetHeight;
                    window.scrollTo({top: document.body.scrollHeight});
                }
                function _scrollToTopForce() {
                    window.scrollTo({top: 0});
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

                    // 在当前流式容器内查找 reasoning badge
                    const container = document.getElementById(_streamingContainerId);
                    if (!container) return;
                    const badge = container.querySelector('.reasoning-badge');
                    if (badge) {
                        if (badge.classList.contains('thinking')) {
                            // thinking badge → 切换为 thought badge
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
                        // 已经是 thought badge，无需改动
                    }

                    _scrollToBottom();
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
                    }

                    _scrollToBottom();
                }

_scrollToBottom();
                _initRoundNav();
