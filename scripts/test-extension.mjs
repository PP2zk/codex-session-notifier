import { runTests } from '@vscode/test-electron';
import { resolve } from 'node:path';
import { existsSync } from 'node:fs';

const executable = process.env.CSN_VSCODE_EXECUTABLE;
if (!executable || !existsSync(executable)) {
  throw new Error('Set CSN_VSCODE_EXECUTABLE to the existing desktop Code executable. This runner never downloads VS Code.');
}
// An agent running inside an editor may inherit Electron's Node-mode flag.
// Scope removal to this test runner and its isolated editor instance.
delete process.env.ELECTRON_RUN_AS_NODE;
delete process.env.VSCODE_IPC_HOOK_CLI;
const previewPort = process.env.CSN_UI_DEBUG_PORT;
if (previewPort && (!/^\d{4,5}$/.test(previewPort) || Number(previewPort) > 65535)) throw new Error('Invalid isolated UI preview port');
await runTests({
  vscodeExecutablePath: executable,
  extensionDevelopmentPath: resolve(process.env.CSN_EXTENSION_DEVELOPMENT_PATH || '.'),
  extensionTestsPath: resolve('out/test/extension-host.js'),
  launchArgs: ['--disable-extensions', '--skip-welcome', '--skip-release-notes', '--disable-workspace-trust', '--disable-telemetry',
    ...(previewPort ? [`--remote-debugging-port=${previewPort}`, '--remote-debugging-address=127.0.0.1'] : []),
    '--user-data-dir', resolve(process.env.CSN_TEST_USER_DATA_DIR || '.vscode-test/user-data'), '--extensions-dir', resolve('.vscode-test/extensions')],
  extensionTestsEnv: { CSN_EXTENSION_TEST: '1' }
});
