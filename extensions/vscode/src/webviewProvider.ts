import * as vscode from 'vscode';

export class LoreWebviewViewProvider implements vscode.WebviewViewProvider {
    public static readonly viewType = 'lore-explorer';
    private _view: vscode.WebviewView | undefined;

    constructor(private readonly _extensionUri: vscode.Uri) {}

    public setLanguageClient(_client: any) {
        // Stats are polled in extension.ts directly from client
    }

    public updateStats(stats: any) {
        if (this._view) {
            this._view.webview.postMessage({ type: 'updateStats', data: stats });
        }
    }

    public resolveWebviewView(
        webviewView: vscode.WebviewView,
        _context: vscode.WebviewViewResolveContext,
        _token: vscode.CancellationToken
    ) {
        this._view = webviewView;
        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: [this._extensionUri]
        };

        webviewView.webview.html = this._getHtmlForWebview();

        webviewView.webview.onDidReceiveMessage(data => {
            switch (data.type) {
                case 'runTask':
                    vscode.window.showInformationMessage(`LORE running task: "${data.value}"`);
                    break;
                case 'acceptChange':
                    vscode.window.showInformationMessage('LORE plan change accepted and committed.');
                    break;
                case 'rejectChange':
                    vscode.window.showWarningMessage('LORE plan change rejected.');
                    if (this._view) {
                        this._view.webview.postMessage({ type: 'hidePlan' });
                    }
                    break;
            }
        });
    }

    private _getHtmlForWebview(): string {
        return `<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>LORE Developer Console</title>
    <style>
        :root {
            --bg-color: #1e1e2e;
            --accent-color: #7c3aed;
            --accent-gradient: linear-gradient(135deg, #7c3aed 0%, #3b82f6 100%);
            --text-color: #cdd6f4;
            --text-muted: #a6adc8;
            --panel-bg: rgba(30, 30, 46, 0.6);
            --border-color: rgba(255, 255, 255, 0.08);
            --success-color: #10b981;
            --warning-color: #f59e0b;
            --danger-color: #ef4444;
        }
        body {
            background-color: var(--bg-color);
            color: var(--text-color);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            margin: 0;
            padding: 16px;
            overflow-x: hidden;
        }
        .header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 24px;
        }
        .title {
            font-size: 1.25rem;
            font-weight: 700;
            background: var(--accent-gradient);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin: 0;
        }
        .status-badge {
            display: flex;
            align-items: center;
            font-size: 0.75rem;
            font-weight: 600;
            background: rgba(16, 185, 129, 0.15);
            color: var(--success-color);
            padding: 4px 8px;
            border-radius: 12px;
            border: 1px solid rgba(16, 185, 129, 0.3);
        }
        .status-dot {
            width: 8px;
            height: 8px;
            background-color: var(--success-color);
            border-radius: 50%;
            margin-right: 6px;
            animation: pulse 2s infinite;
        }
        @keyframes pulse {
            0% { transform: scale(0.95); opacity: 0.5; }
            50% { transform: scale(1.1); opacity: 1; }
            100% { transform: scale(0.95); opacity: 0.5; }
        }
        .card {
            background: var(--panel-bg);
            backdrop-filter: blur(10px);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 16px;
            margin-bottom: 16px;
            box-shadow: 0 4px 30px rgba(0, 0, 0, 0.1);
        }
        .card-title {
            font-size: 0.875rem;
            font-weight: 600;
            color: var(--text-muted);
            margin-top: 0;
            margin-bottom: 12px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }
        .stat-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
            margin-bottom: 16px;
        }
        .stat-box {
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 10px;
            text-align: center;
        }
        .stat-val {
            font-size: 1.125rem;
            font-weight: 700;
            color: #ffffff;
        }
        .stat-lbl {
            font-size: 0.7rem;
            color: var(--text-muted);
            margin-top: 4px;
        }
        textarea {
            width: 100%;
            min-height: 80px;
            background: rgba(0, 0, 0, 0.2);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            color: #ffffff;
            padding: 10px;
            box-sizing: border-box;
            resize: vertical;
            font-family: inherit;
            margin-bottom: 12px;
            transition: border-color 0.2s;
        }
        textarea:focus {
            outline: none;
            border-color: #7c3aed;
            box-shadow: 0 0 0 2px rgba(124, 58, 237, 0.2);
        }
        .btn {
            width: 100%;
            background: var(--accent-gradient);
            color: #ffffff;
            border: none;
            border-radius: 8px;
            padding: 10px;
            font-weight: 600;
            cursor: pointer;
            transition: transform 0.1s, opacity 0.2s;
            box-shadow: 0 4px 12px rgba(124, 58, 237, 0.3);
        }
        .btn:hover {
            opacity: 0.9;
        }
        .btn:active {
            transform: scale(0.98);
        }
        .plan-section {
            border-left: 3px solid var(--warning-color);
            padding-left: 12px;
            margin-bottom: 16px;
        }
        .plan-title {
            font-weight: 600;
            color: #ffffff;
            font-size: 0.9rem;
        }
        .plan-meta {
            font-size: 0.8rem;
            color: var(--text-muted);
            margin-top: 4px;
        }
        .btn-group {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
            margin-top: 12px;
        }
        .btn-secondary {
            background: rgba(255, 255, 255, 0.08);
            border: 1px solid var(--border-color);
            color: #ffffff;
            box-shadow: none;
        }
        .btn-secondary:hover {
            background: rgba(255, 255, 255, 0.15);
        }
        .btn-danger {
            background: var(--danger-color);
            box-shadow: 0 4px 12px rgba(239, 68, 68, 0.3);
        }
        .btn-success {
            background: var(--success-color);
            box-shadow: 0 4px 12px rgba(16, 185, 129, 0.3);
        }
    </style>
</head>
<body>
    <div class="header">
        <h2 class="title">LORE Lens</h2>
        <div class="status-badge" id="status-badge">
            <div class="status-dot"></div>
            <span>ACTIVE</span>
        </div>
    </div>

    <div class="card">
        <div class="card-title">Institutional Memory</div>
        <div class="stat-grid">
            <div class="stat-box">
                <div class="stat-val" id="symbolCount">0</div>
                <div class="stat-lbl">Symbols</div>
            </div>
            <div class="stat-box">
                <div class="stat-val" id="linkCount">0</div>
                <div class="stat-lbl">Decision Links</div>
            </div>
        </div>
    </div>

    <div class="card">
        <div class="card-title">Run Engineering Task</div>
        <textarea id="taskInput" placeholder="Aggiungi logging a tutte le funzioni db..."></textarea>
        <button class="btn" id="runBtn">Execute Agent</button>
    </div>

    <div class="card" id="dryRunCard" style="display:none">
        <div class="card-title">Plan Dry-Run</div>
        <div class="plan-section">
            <div class="plan-title">Update Winston logger configurations</div>
            <div class="plan-meta">Confidence: <span style="color:var(--success-color); font-weight:bold;">87%</span> (ADR-003)</div>
            <div class="plan-meta">Blast Radius: <span style="color:var(--warning-color); font-weight:bold;">6 test suites</span></div>
        </div>
        <div class="btn-group">
            <button class="btn btn-secondary btn-danger" id="rejectBtn">Reject</button>
            <button class="btn btn-secondary btn-success" id="acceptBtn">Accept</button>
        </div>
    </div>

    <script>
        const vscode = acquireVsCodeApi();
        
        document.getElementById('runBtn').addEventListener('click', () => {
            const val = document.getElementById('taskInput').value.trim();
            if (val) {
                vscode.postMessage({ type: 'runTask', value: val });
                setTimeout(() => {
                    document.getElementById('dryRunCard').style.display = 'block';
                }, 1500);
            }
        });

        document.getElementById('acceptBtn').addEventListener('click', () => {
            vscode.postMessage({ type: 'acceptChange' });
            document.getElementById('dryRunCard').style.display = 'none';
        });

        document.getElementById('rejectBtn').addEventListener('click', () => {
            vscode.postMessage({ type: 'rejectChange' });
            document.getElementById('dryRunCard').style.display = 'none';
        });

        window.addEventListener('message', event => {
            const msg = event.data;
            if (msg.type === 'updateStats') {
                document.getElementById('symbolCount').textContent = msg.data.symbols;
                document.getElementById('linkCount').textContent = msg.data.decision_links;
            } else if (msg.type === 'hidePlan') {
                document.getElementById('dryRunCard').style.display = 'none';
            }
        });
    </script>
</body>
</html>`;
    }
}
