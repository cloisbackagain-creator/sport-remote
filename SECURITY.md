# Security

Found a security problem in Sport Remote? Please report it privately through
**Security → Report a vulnerability** on this repository rather than opening a public issue.

How it's protected:

- **Web app:** no third-party code or requests; a Content Security Policy allows only `app.js`
  to run, and text from the device or guests is escaped before display.
- **PC bridge:** listens on `127.0.0.1` only and refuses other websites (Origin/Host checks).
- **Sharing:** each guest has a personal invite link with a random 256-bit key (kept after `#`,
  so it never reaches a server). Links can expire or be revoked; wrong keys lock an address out
  for 15 minutes. Guests can only start/cancel sessions or watch; settings, presets, lights,
  recalibration and power stay with the host. Sharing stops when the PC app closes.
- **Dependencies:** pinned in `requirements.txt`.
