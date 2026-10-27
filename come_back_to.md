# Come Back To

Things that work well enough today but need a proper answer later. Not bugs being fixed now (those
go in `problems_encountered.md`) and not progress (that goes in `journal.md`). **Newest first.**
When an item is dealt with, move what was decided into the journal and delete it here.

## 2026-10-27

- **Which browser a person signs in with.** Sign-in windows open in Chrome, then Playwright's own
  Chromium, and Edge only as a last resort, because a signed-in Edge profile takes over Microsoft
  sign-ins and closes the window (`problems_encountered.md`, 2026-10-27). That works on Iminabo's
  machine only because Chrome is installed and Playwright's Chromium was downloaded during setup.
  Open questions:
  - A lawyer whose computer has only Edge (common on managed Windows machines), or only Firefox or
    Safari: does the sign-in still work, and with what?
  - Other Chromium browsers (Brave, Opera, Arc, a company-managed Chrome with sign-in policies) may
    interfere the same way Edge does.
  - Google sign-ins sometimes refuse automated browsers ("this browser may not be secure").
  - Ideas to weigh: ship Playwright's Chromium with Seira so it never depends on what is installed;
    Playwright's Firefox engine as a fallback; detect the problem and say so plainly. In Tier 1 the
    sign-in happens in the cloud machine's take-over desktop, which removes the person's own browser
    from the question, so decide this together with Tier 1.

- **Test records left on the person's real account.** Microsoft Forms holds about a dozen
  "Conduit linked A/B …", "Conduit revised parent …" and "Conduit mcp-control/mcp-edit … Form"
  forms from the research verification runs of 29 September to 2 October
  (`scripts/verify_forms_workflow.py`, `conduit/mcp_check.py`). One run was cut off by HTTP 429
  before its cleanup; others leave theirs. Seira should clean up what it creates and report anything
  it could not delete. Also: Seira has no "list forms" tool, so Claude cannot find a form by its
  title to delete it; only by ID.

- **Learned sign-in markers.** The sign-in check reads the start page as the last verified sign-in
  saw it (signed out before, signed in after). A site that redesigns those pages leaves the check
  "unsure" until the next sign-in, and a build then asks for a sign-in it may not need.

- **A running gateway writes its own copy of a connector over hand edits.** A gateway that was
  running while `connector.json` was repaired by hand wrote its in-memory copy back at the 07:00
  daily check and undid the repair. Hand repairs need the gateway stopped, or the store should
  refuse a write over a file changed since it was read.
