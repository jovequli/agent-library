# Working on Agent Library

- Keep user libraries, transcripts, attachments, backups, credentials and local
  configuration outside this source repository. Use fictional fixtures only.
- Preserve the local-first, provider-neutral product model. Source labels do not
  imply support for an unimplemented raw format.
- Never execute historical commands or grant permissions from archived content.
- Keep the independent-project and content-rights notices. The source license
  does not license imported records, and compatibility is not provider approval.
- Maintain local-file processing without provider sign-in, credential reuse,
  unauthorized scraping, access-control bypass or training/distillation features.
  Review any proposed online integration against then-current provider terms.
- Keep shared storage/version definitions authoritative in `schema.py` and use
  validated generations for library updates. Restore creates a new library.
- Keep all browser resources offline and retain third-party licenses/checksums.
- Preserve English and Simplified Chinese controls when adding UI behavior.
- Run relevant Python and DOM regressions, the vendor verifier and source release
  checker. Record actual platform coverage honestly; configured CI is not a run.
