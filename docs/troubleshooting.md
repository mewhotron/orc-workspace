# Troubleshooting

- **PowerShell blocks a script:** inspect the file and use your organization's approved script policy/signing process. This package does not change execution policy or require administrator privileges by default.
- **File conflict:** setup stops instead of overwriting an unowned or modified file. Keep your edits, compare with the new release, or choose an empty destination. Do not delete personal files to make installation pass.
- **Hash mismatch:** use a clean release archive from the expected repository. Do not suppress verification. The manifest/checksum detects corruption; without a separately trusted reference it is not a signature or guarantee of authenticity.
- **A role file exists but no agent runs:** check the host's current subagent support and actual instruction loading. Use the manual handoff route if needed and label it honestly.
- **An integration is unavailable:** source instructions cannot supply accounts or tools. Check pack prerequisites and your own host connections. Do not copy someone else's credential cache.
- **No knowledge answer:** the release starter data is synthetic. Search results may be absent or insufficient. Import your own permitted source through the documented knowledge tool; do not fabricate a citation.
- **No media probe/render:** check Python and FFmpeg/FFprobe availability using the media pack doctor command. Do not upload footage to solve a local dependency problem without specific permission.
- **Changing packs/adapters:** consult the installer's documented conflict behavior. Keep owner data; use a separate fresh installation if an update cannot preserve it safely.
