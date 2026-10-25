# BrowserWindows

A temporary browser-accessible Windows desktop prototype powered by GitHub Actions.

## What it does

- Starts a temporary **Windows Server 2022** GitHub-hosted runner.
- Captures the desktop with FFmpeg and encodes low-latency H.264.
- Sends fragmented MP4 over a WebSocket, so video travels through HTTPS/TCP and can pass through a standard Cloudflare Quick Tunnel. This avoids relying on a direct WebRTC/UDP media connection.
- Supports browser mouse and keyboard input.
- Protects the browser session with a password login.
- Uses a temporary Cloudflare Quick Tunnel; the session ends when the Actions job is cancelled or times out.

## Important limitations

**This is not a Windows 10 VM.** GitHub's standard hosted Windows runner is Windows Server 2022. Running an actual Windows 10 virtual machine inside a hosted runner is not guaranteed because nested virtualization, GPU acceleration, and enough disk space are not guaranteed.

This first prototype streams video and mouse/keyboard input. System audio and clipboard synchronization are not implemented yet. The runner may also provide a locked or unavailable desktop capture surface; if FFmpeg cannot capture the runner desktop, the session will report a stream error. We need a real Actions run to verify that.

The stream uses H.264 fragmented MP4 over WebSocket/TCP rather than noVNC. It should be more efficient for moving screens, but browser/runner constraints mean smoothness and latency must be measured in the actual session. It is not WebRTC and does not promise the same latency as a direct WebRTC connection.

## Setup

### 1. Add a password secret

In this repository, open **Settings → Secrets and variables → Actions → New repository secret**.

Create:

- **Name:** `DESKTOP_PASSWORD`
- **Value:** a strong, unique password of at least 12 characters.

Do not put the password in the workflow file or commit it to the repository. The repository is public, so anyone can read the source and workflow logs. The desktop URL can be visible in Actions logs; the password must remain a secret.

### 2. Start a session

1. Open the repository's **Actions** tab.
2. Select **Start BrowserWindows session**.
3. Click **Run workflow** and confirm.
4. Wait for the step to display the temporary `trycloudflare.com` URL in the workflow summary.
5. Open the URL and enter your `DESKTOP_PASSWORD` secret value.

The workflow runs for up to 350 minutes. To end the session, open the running workflow and click **Cancel workflow**. The hosted runner and its temporary files are then discarded.

## Troubleshooting

- **The workflow fails because the secret is missing:** add `DESKTOP_PASSWORD` under repository Actions secrets.
- **The tunnel starts but video fails:** open the workflow logs and look for FFmpeg or desktop-capture errors. Hosted runners are CI machines, not guaranteed interactive cloud PCs.
- **The browser says H.264/MSE is unsupported:** try a current version of Chrome, Edge, or Firefox.
- **The desktop URL loads but login does not persist:** check that you opened the HTTPS tunnel URL, not the local HTTP URL.

## Security notes

- Do not remove the login check or expose the local streaming port without authentication.
- Anyone who obtains the password can control the remote desktop.
- Cancel the workflow when finished. The session is temporary and should not be used for sensitive accounts or private data.
- Use only software and content you are authorized to access.

## License

This project currently has no explicit repository license.
