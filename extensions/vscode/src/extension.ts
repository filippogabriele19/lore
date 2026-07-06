import * as path from 'path';
import * as fs from 'fs';
import * as vscode from 'vscode';
import {
    LanguageClient,
    LanguageClientOptions,
    ServerOptions
} from 'vscode-languageclient/node';
import { LoreWebviewViewProvider } from './webviewProvider';

let client: LanguageClient | undefined;
let statsInterval: NodeJS.Timeout | undefined;

export function activate(context: vscode.ExtensionContext) {
    const provider = new LoreWebviewViewProvider(context.extensionUri);
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider(LoreWebviewViewProvider.viewType, provider)
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('lore.startLSP', () => startLanguageServer(provider)),
        vscode.commands.registerCommand('lore.stopLSP', stopLanguageServer),
        vscode.commands.registerCommand('lore.restartLSP', async () => {
            await stopLanguageServer();
            await startLanguageServer(provider);
        })
    );

    startLanguageServer(provider);
}

export async function deactivate(): Promise<void> {
    await stopLanguageServer();
}

async function startLanguageServer(provider?: LoreWebviewViewProvider): Promise<void> {
    if (client) {
        return;
    }

    const workspaceFolders = vscode.workspace.workspaceFolders;
    if (!workspaceFolders) {
        vscode.window.showErrorMessage('LORE requires an open workspace folder.');
        return;
    }

    const rootPath = workspaceFolders[0].uri.fsPath;
    const pythonPath = getPythonPath(rootPath);
    const scriptPath = path.join(rootPath, 'lore.py');

    if (!fs.existsSync(scriptPath)) {
        vscode.window.showErrorMessage(`LORE entry point script not found at ${scriptPath}`);
        return;
    }

    const serverOptions: ServerOptions = {
        command: pythonPath,
        args: [scriptPath, 'lsp'],
        options: {
            cwd: rootPath,
            env: { ...process.env, PYTHONUNBUFFERED: '1' }
        }
    };

    const clientOptions: LanguageClientOptions = {
        documentSelector: [
            { scheme: 'file', language: 'python' },
            { scheme: 'file', language: 'go' },
            { scheme: 'file', language: 'typescript' },
            { scheme: 'file', language: 'javascript' }
        ],
        synchronize: {
            fileEvents: vscode.workspace.createFileSystemWatcher('**/*')
        }
    };

    client = new LanguageClient(
        'loreLanguageServer',
        'LORE Language Server',
        serverOptions,
        clientOptions
    );

    try {
        await client.start();
        vscode.window.showInformationMessage('LORE Language Server started successfully.');
        if (provider) {
            provider.setLanguageClient(client);
            if (statsInterval) {
                clearInterval(statsInterval);
            }
            statsInterval = setInterval(async () => {
                if (client && client.isRunning()) {
                    try {
                        const stats = await client.sendRequest('lore/getStats');
                        provider.updateStats(stats);
                    } catch {
                        // ignore server non-readiness
                    }
                }
            }, 10000);
        }
    } catch (e: any) {
        vscode.window.showErrorMessage(`Failed to start LORE Language Server: ${e.message}`);
        client = undefined;
    }
}

async function stopLanguageServer(): Promise<void> {
    if (statsInterval) {
        clearInterval(statsInterval);
        statsInterval = undefined;
    }
    if (!client) {
        return;
    }
    try {
        await client.stop();
    } catch (e) {
        // Suppress errors during client stop
    }
    client = undefined;
    vscode.window.showInformationMessage('LORE Language Server stopped.');
}

function getPythonPath(rootPath: string): string {
    const config = vscode.workspace.getConfiguration('lore');
    const customPath = config.get<string>('pythonPath');
    if (customPath && customPath.trim().length > 0) {
        return customPath;
    }

    const winVenv = path.join(rootPath, 'venv', 'Scripts', 'python.exe');
    const unixVenv = path.join(rootPath, 'venv', 'bin', 'python');

    if (process.platform === 'win32' && fs.existsSync(winVenv)) {
        return winVenv;
    } else if (fs.existsSync(unixVenv)) {
        return unixVenv;
    }

    return 'python';
}
