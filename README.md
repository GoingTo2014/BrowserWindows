# BrowserWindows

A temporary browser-accessible Windows desktop prototype powered by GitHub Actions.

## What it does

- Starts a temporary **Windows Server 2022** GitHub-hosted runner.
- Captures the desktop with FFmpeg and encodes low-latency H.264.
- Tries an experimental WebRTC video path first, using H.264 and STUN-assisted ICE connectivity.
- Automatically falls back to the existing fragmented MP4 over WebSocket/TCP path when WebRTC negotiation or connectivity fails. The TCP path works through a standard Cloudflare Quick Tunnel.
- Supports browser mouse and keyboard input.
- Protects the browser session with a password login.
- Uses a temporary Cloudflare Quick Tunnel; the session ends when the Actions job is cancelled or times out.

## Important limitations

**This is not a Windows 10 VM.** GitHub's standard hosted Windows runner is Windows Server 2022. Running an actual Windows 10 virtual machine inside a hosted runner is not guaranteed because nested virtualization, GPU acceleration, and enough disk space are not guaranteed.

This first prototype streams video and mouse/keyboard input. System audio and clipboard synchronization are not implemented yet. The runner may also provide a locked or unavailable desktop capture surface; if FFmpeg cannot capture the runner desktop, the session will report a stream error. We need a real Actions run to verify that.

The experimental WebRTC path captures desktop frames with FFmpeg and passes them to aiortc for H.264 encoding. It uses STUN to discover candidate routes, but STUN alone cannot guarantee connectivity through the GitHub runner's NAT/firewall. The browser automatically falls back to the existing H.264 fragmented-MP4 WebSocket/TCP stream if negotiation fails or the UDP path does not connect. Direct UDP has not yet been verified on the hosted runner, and this change must be tested with a real Actions session. WebRTC may also use a non-UDP candidate depending on network conditions; the UI reports WebRTC when connected, not a guaranteed UDP route.

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
- **WebRTC never connects:** the browser should switch to TCP automatically. The runner or network may block direct UDP; the TCP fallback remains available.
- **The browser says H.264/MSE is unsupported:** try a current version of Chrome, Edge, or Firefox.
- **WebRTC reports no H.264 encoder:** check the Python dependency installation logs; the experimental WebRTC path needs compatible aiortc/PyAV H.264 support.
- **The desktop URL loads but login does not persist:** check that you opened the HTTPS tunnel URL, not the local HTTP URL.

## Security notes

- Do not remove the login check or expose the local streaming port without authentication.
- Anyone who obtains the password can control the remote desktop.
- Cancel the workflow when finished. The session is temporary and should not be used for sensitive accounts or private data.
- Use only software and content you are authorized to access.

## License

This project currently has no explicit repository license.
