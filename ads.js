document.addEventListener('DOMContentLoaded', function() {
    const adsExportButton = document.getElementById('adsExportButton');
    const adsExportStatusCard = document.getElementById('adsExportStatusCard');
    const adsStatusBadge = document.getElementById('adsStatusBadge');
    const adsStatusStep = document.getElementById('adsStatusStep');
    const adsProgressBar = document.getElementById('adsProgressBar');
    const adsProgressText = document.getElementById('adsProgressText');
    const adsStatusMessage = document.getElementById('adsStatusMessage');
    const adsExportResult = document.getElementById('adsExportResult');
    const adsAnalyzeButton = document.getElementById('adsAnalyzeButton');
    const includeAiAnalysis = document.getElementById('includeAiAnalysis');
    const openaiStatus = document.getElementById('openaiStatus');
    const openaiModel = document.getElementById('openaiModel');
    const openaiReasoningEffort = document.getElementById('openaiReasoningEffort');
    const adsAnalysisStatusCard = document.getElementById('adsAnalysisStatusCard');
    const adsAnalysisStatusBadge = document.getElementById('adsAnalysisStatusBadge');
    const adsAnalysisStatusStep = document.getElementById('adsAnalysisStatusStep');
    const adsAnalysisProgressBar = document.getElementById('adsAnalysisProgressBar');
    const adsAnalysisProgressText = document.getElementById('adsAnalysisProgressText');
    const adsAnalysisStatusMessage = document.getElementById('adsAnalysisStatusMessage');
    const adsAnalysisReport = document.getElementById('adsAnalysisReport');

    window.adsExportRunning = false;
    window.adsAnalysisRunning = false;
    let openaiStatusData = null;

    function escapeHtml(value) {
        return String(value ?? '')
            .replaceAll('&', '&amp;')
            .replaceAll('<', '&lt;')
            .replaceAll('>', '&gt;')
            .replaceAll('"', '&quot;')
            .replaceAll("'", '&#039;');
    }

    function syncAiControlState() {
        const aiEnabled = includeAiAnalysis ? includeAiAnalysis.checked : true;
        openaiModel.disabled = !aiEnabled || window.adsAnalysisRunning;
        openaiReasoningEffort.disabled = !aiEnabled || window.adsAnalysisRunning;
    }

    function renderOpenAIStatus(data) {
        openaiStatusData = data;
        if (data.configured) {
            openaiStatus.className = 'openai-status configured';
            openaiStatus.innerHTML = `<i class="fas fa-circle-check"></i> OpenAI API 已設定（${escapeHtml(data.key_source)}）；報告會顯示實際回應模型。`;
        } else {
            openaiStatus.className = 'openai-status missing';
            openaiStatus.innerHTML = `<i class="fas fa-triangle-exclamation"></i> 尚未設定 API Key。請在專案終端執行 <code>${escapeHtml(data.setup_command || 'python3 setup_openai_key.py')}</code>。`;
        }
        if (data.default_model && openaiModel.querySelector(`option[value="${CSS.escape(data.default_model)}"]`)) {
            openaiModel.value = data.default_model;
        }
        if (data.default_reasoning_effort && openaiReasoningEffort.querySelector(`option[value="${CSS.escape(data.default_reasoning_effort)}"]`)) {
            openaiReasoningEffort.value = data.default_reasoning_effort;
        }
        syncAiControlState();
    }

    function loadOpenAIStatus() {
        fetch('/openai_status')
            .then(async (response) => {
                const data = await response.json();
                if (!response.ok || data.status === 'error') {
                    throw new Error(data.message || '無法檢查 OpenAI 設定');
                }
                return data;
            })
            .then(renderOpenAIStatus)
            .catch((error) => {
                openaiStatusData = null;
                openaiStatus.className = 'openai-status error';
                openaiStatus.innerHTML = `<i class="fas fa-circle-exclamation"></i> ${escapeHtml(error.message || '無法檢查 OpenAI 設定')}`;
            });
    }

    function updateAdsExportUI(step, message, progress) {
        adsExportStatusCard.style.display = 'block';
        adsStatusStep.textContent = step;
        adsStatusMessage.textContent = message;
        adsProgressBar.style.width = `${progress}%`;
        adsProgressText.textContent = `${Math.round(progress)}%`;
    }

    function resetAdsExportUI() {
        adsExportStatusCard.style.display = 'none';
        adsStatusBadge.textContent = '執行中';
        adsStatusBadge.className = 'ads-status-badge';
        adsStatusStep.textContent = '準備開始...';
        adsStatusMessage.textContent = '等待後端啟動流程...';
        adsProgressBar.style.width = '0%';
        adsProgressText.textContent = '0%';
        adsExportResult.style.display = 'none';
        adsExportResult.className = 'ads-export-result';
        adsExportResult.innerHTML = '';
    }

    function updateAdsAnalysisUI(step, message, progress) {
        adsAnalysisStatusCard.style.display = 'block';
        adsAnalysisStatusStep.textContent = step;
        adsAnalysisStatusMessage.textContent = message;
        adsAnalysisProgressBar.style.width = `${progress}%`;
        adsAnalysisProgressText.textContent = `${Math.round(progress)}%`;
    }

    function resetAdsAnalysisUI() {
        adsAnalysisStatusCard.style.display = 'none';
        adsAnalysisStatusBadge.textContent = '分析中';
        adsAnalysisStatusBadge.className = 'ads-status-badge';
        adsAnalysisStatusStep.textContent = '準備解析報表...';
        adsAnalysisStatusMessage.textContent = '正在等待分析器啟動...';
        adsAnalysisProgressBar.style.width = '0%';
        adsAnalysisProgressText.textContent = '0%';
        adsAnalysisReport.style.display = 'none';
        adsAnalysisReport.innerHTML = '';
    }

    function setAdsButtonsDisabled(disabled) {
        adsExportButton.disabled = disabled;
        adsAnalyzeButton.disabled = disabled;
        syncAiControlState();
    }

    function startAdsExportProgressSimulation() {
        const steps = [
            { progress: 10, step: '登入中', message: '正在使用現有 cookies 進入蝦皮賣家中心...' },
            { progress: 28, step: '進入廣告後台', message: '正在尋找並點擊蝦皮廣告入口...' },
            { progress: 46, step: '批次導出', message: '正在依序處理過去一個月、昨天與近 4 週趨勢，共 6 份...' },
            { progress: 68, step: '輪詢報表', message: '正在檢查是否已有可下載報表，避免重複導出...' },
            { progress: 84, step: '等待下載', message: '若報表仍在處理中，系統會持續輪詢直到可下載...' },
            { progress: 95, step: '整理檔案', message: '正在保存檔案並整理批次結果...' }
        ];

        let index = 0;
        updateAdsExportUI('登入中', '正在使用現有 cookies 進入蝦皮賣家中心...', 5);
        return setInterval(() => {
            if (index >= steps.length) return;
            const current = steps[index];
            updateAdsExportUI(current.step, current.message, current.progress);
            index += 1;
        }, 2500);
    }

    function startAdsAnalysisProgressSimulation() {
        const steps = [
            { progress: 10, step: '載入來源', message: '正在讀取現有昨天、過去一個月與過去 4 週滾動周報...' },
            { progress: 26, step: '解析 CSV', message: '正在拆解 Shopee 報表 metadata 與正式表頭...' },
            { progress: 42, step: '標準化資料', message: '正在整理商品 ID、花費、銷售與直接轉換指標...' },
            { progress: 58, step: '周趨勢建模', message: '正在計算近 4 週 ROAS、CTR、CVR、CPC、CPA 趨勢...' },
            { progress: 76, step: '映射商品圖', message: '正在將商品 ID 對應到 golden_table.json 的圖片與名稱...' },
            { progress: 90, step: '生成報告', message: '正在整理當前快照 + 4 週趨勢卡片與 AI 建議...' }
        ];

        let index = 0;
        updateAdsAnalysisUI('解析 CSV', '正在拆解 Shopee 報表 metadata 與正式表頭...', 5);
        return setInterval(() => {
            if (index >= steps.length) return;
            const current = steps[index];
            updateAdsAnalysisUI(current.step, current.message, current.progress);
            index += 1;
        }, 2200);
    }

    function showAdsExportResult(type, html) {
        adsExportResult.style.display = 'block';
        adsExportResult.className = `ads-export-result ${type}`;
        adsExportResult.innerHTML = html;
    }

    function renderAdsBatchResults(data) {
        const results = Array.isArray(data.results) ? data.results : [];
        const summaryTitle =
            data.status === 'partial_success' ? '部分完成' :
            data.status === 'error' ? '執行失敗' : '下載完成';
        const rows = results.map((item) => {
            const fileText = item.file_name ? `<br>檔名：${item.file_name}` : '';
            const actionText = item.action_taken ? `<br>動作：${item.action_taken}` : '';
            const messageText = item.message ? `<br>訊息：${item.message}` : '';
            const cssClass =
                item.status === 'success' ? 'success' :
                item.status === 'skipped' ? 'warning' : 'error';
            return `<div class="ads-result-item ${cssClass}">
                <strong>${item.range_label}</strong>
                <br>狀態：${item.status}
                ${fileText}
                ${actionText}
                ${messageText}
            </div>`;
        }).join('');
        return `<strong>${summaryTitle}</strong><br>${data.message}<div class="ads-result-list">${rows}</div>`;
    }

    function renderAdsAnalysisReport(data) {
        const report = data.report || {};
        const summary = report.account_summary || {};
        const current = summary.current_window || {};
        const narrative = report.narrative || {};
        const rankings = report.rankings || {};
        const runtime = data.analysis_runtime || {};
        const files = data.output_files || {};
        const htmlPath = files.html_report ? `/${files.html_report.split('/').pop()}` : '';
        const actionableCount =
            (rankings.scale_up || []).length +
            (rankings.reduce_budget || []).length +
            (rankings.indirect_dependency || []).length +
            (rankings.watchlist || []).length;
        const excluded = Array.isArray(narrative.excluded_but_reviewed_products)
            ? narrative.excluded_but_reviewed_products
            : [];
        const watchlistCount = (rankings.watchlist || []).length;

        return `
            <div class="ads-analysis-summary-grid">
                <div class="ads-analysis-summary-card">
                    <span>主決策基準</span>
                    <strong>${current.window_label || '昨天'}</strong>
                    <p>已額外納入過去 4 週滾動趨勢</p>
                </div>
                <div class="ads-analysis-summary-card">
                    <span>昨日 ROAS</span>
                    <strong>${Number(current.roas || 0).toFixed(2)}</strong>
                    <p>直接 ROAS ${Number(current.direct_roas || 0).toFixed(2)}</p>
                </div>
                <div class="ads-analysis-summary-card">
                    <span>需處理商品數</span>
                    <strong>${actionableCount}</strong>
                    <p>包含加碼、降預算、間接轉換與先觀察</p>
                </div>
                <div class="ads-analysis-summary-card">
                    <span>分析來源</span>
                    <strong>${narrative.source === 'openai' ? escapeHtml(runtime.response_model || runtime.model || 'OpenAI API') : '本機規則'}</strong>
                    <p>${data.has_ai_enhancement ? `${escapeHtml(runtime.reasoning_effort || '-')} 推理・${Number(runtime.api_latency_seconds || 0).toFixed(1)} 秒・${runtime.attempts || 1} 次` : '此次未呼叫 OpenAI API'}</p>
                </div>
                <div class="ads-analysis-summary-card">
                    <span>先觀察</span>
                    <strong>${watchlistCount}</strong>
                    <p>昨天異常但週趨勢未必持續轉弱</p>
                </div>
            </div>

            <div class="ads-analysis-section">
                <h3><i class="fas fa-file-lines"></i> HTML 報告已生成</h3>
                <p>${escapeHtml(narrative.executive_summary || data.message || '廣告分析完成。')}</p>
                <ul class="ads-analysis-action-list">
                    ${(narrative.next_actions || []).map((item) => `<li>${escapeHtml(item)}</li>`).join('')}
                </ul>
                <div class="ads-analysis-files">
                    <div><strong>HTML：</strong>${files.html_report || '-'}</div>
                    <div><strong>分析 JSON：</strong>${files.analysis_json || '-'}</div>
                    <div><strong>歷史 JSON：</strong>${files.history_json || '-'}</div>
                </div>
                ${htmlPath ? `<a class="btn-primary ads-report-download" href="${htmlPath}" target="_blank" rel="noopener noreferrer"><i class="fas fa-file-lines"></i> 開啟 HTML 報告</a>` : ''}
            </div>

            ${excluded.length ? `
            <div class="ads-analysis-section">
                <h3><i class="fas fa-eye"></i> 已檢查但暫不列入</h3>
                <ul class="ads-analysis-action-list">
                    ${excluded.slice(0, 10).map((item) => `<li>${escapeHtml(item.product_id)}: ${escapeHtml(item.reason)}</li>`).join('')}
                </ul>
            </div>` : ''}
        `;
    }

    function performAdsExport() {
        if (window.adsExportRunning) return;
        const headlessModeElement = document.getElementById('headlessMode');
        const showBrowser = headlessModeElement ? headlessModeElement.checked : true;
        const exportUrl = `/export_ads?showBrowser=${showBrowser}`;

        window.adsExportRunning = true;
        resetAdsExportUI();
        setAdsButtonsDisabled(true);

        const progressInterval = startAdsExportProgressSimulation();
        fetch(exportUrl)
            .then(async (response) => {
                const data = await response.json();
                if (!response.ok || data.status === 'error') {
                    throw new Error(data.message || '廣告匯出失敗');
                }
                return data;
            })
            .then((data) => {
                clearInterval(progressInterval);
                const isPartial = data.status === 'partial_success';
                adsStatusBadge.textContent = isPartial ? '部分完成' : '已完成';
                adsStatusBadge.className = `ads-status-badge ${isPartial ? 'warning' : 'success'}`;
                updateAdsExportUI(isPartial ? '部分完成' : '下載完成', data.message || '廣告報表已成功下載並完成重新命名。', 100);
                showAdsExportResult(isPartial ? 'warning' : 'success', renderAdsBatchResults(data));
            })
            .catch((error) => {
                clearInterval(progressInterval);
                adsStatusBadge.textContent = '失敗';
                adsStatusBadge.className = 'ads-status-badge error';
                updateAdsExportUI('匯出失敗', error.message || '廣告匯出失敗', 100);
                showAdsExportResult('error', `<strong>匯出失敗</strong><br>${error.message || '請稍後再試'}`);
            })
            .finally(() => {
                window.adsExportRunning = false;
                setAdsButtonsDisabled(false);
            });
    }

    function performAdsAnalysis() {
        if (window.adsAnalysisRunning) return;
        const includeAI = includeAiAnalysis ? includeAiAnalysis.checked : true;
        if (includeAI && (!openaiStatusData || !openaiStatusData.configured)) {
            adsAnalysisReport.style.display = 'block';
            adsAnalysisReport.innerHTML = '<div class="ads-analysis-error"><strong>尚未連接 OpenAI API</strong><br>請先在專案終端執行 <code>python3 setup_openai_key.py</code>，完成後重新整理這個廣告頁。API Key 不要貼到聊天或網頁中。</div>';
            return;
        }
        const query = new URLSearchParams({
            includeAI: String(includeAI),
            model: openaiModel.value,
            reasoningEffort: openaiReasoningEffort.value,
        });
        const analysisUrl = `/analyze_ads?${query.toString()}`;

        window.adsAnalysisRunning = true;
        resetAdsAnalysisUI();
        setAdsButtonsDisabled(true);

        const progressInterval = startAdsAnalysisProgressSimulation();
        fetch(analysisUrl)
            .then(async (response) => {
                const data = await response.json();
                if (!response.ok || data.status === 'error') {
                    throw new Error(data.message || '廣告分析失敗');
                }
                return data;
            })
            .then((data) => {
                clearInterval(progressInterval);
                adsAnalysisStatusBadge.textContent = '已完成';
                adsAnalysisStatusBadge.className = 'ads-status-badge success';
                updateAdsAnalysisUI('分析完成', data.message || '已完成廣告分析報告', 100);
                adsAnalysisReport.style.display = 'block';
                adsAnalysisReport.innerHTML = renderAdsAnalysisReport(data);
            })
            .catch((error) => {
                clearInterval(progressInterval);
                adsAnalysisStatusBadge.textContent = '失敗';
                adsAnalysisStatusBadge.className = 'ads-status-badge error';
                updateAdsAnalysisUI('分析失敗', error.message || '請稍後再試', 100);
                adsAnalysisReport.style.display = 'block';
                adsAnalysisReport.innerHTML = `<div class="ads-analysis-error"><strong>分析失敗</strong><br>${escapeHtml(error.message || '請稍後再試')}</div>`;
            })
            .finally(() => {
                window.adsAnalysisRunning = false;
                setAdsButtonsDisabled(false);
            });
    }

    adsExportButton.addEventListener('click', performAdsExport);
    adsAnalyzeButton.addEventListener('click', performAdsAnalysis);
    includeAiAnalysis.addEventListener('change', syncAiControlState);
    loadOpenAIStatus();

});
