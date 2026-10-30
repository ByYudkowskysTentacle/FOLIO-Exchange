# Wording guide

How the desktop app's text should read. It applies to every word a person sees or hears in the app (`src/folio_exchange/desktop/`): labels, buttons, help under fields, notices, problems, dialogs, table headers, sidebar entries, tooltips, window titles, placeholder text, and accessible names and descriptions.

Follow it for new and changed text. Messages from the shared core (`setupops.py`, `views.py`, `feedops.py`, `runner.py` and others) also reach the browser page and the command line, and they don't follow this guide yet. Neither do the browser page and the docs. Log messages, docstrings and comments are out of scope.

## Voice

- Plain US English with contractions: "doesn't", "can't", "isn't", "you'll", "it's". Keep the full form only for emphasis in a warning, where "do not" is standard.
- Sentence case for everything, as FOLIO does.
- Address the reader as "you". Use the imperative for instructions.
- Brief. Keep what the librarian needs to act or to stay safe, and cut the rest. Help under a field is one short sentence when it can be, two at most.

## Habits to avoid

The app's text was first written by an AI model and carried its habits. These are the ones to watch for.

1. **Clauses joined by colons.** Use two sentences, a comma, or "so" or "because" when the link matters. Keep colons for a label before its value ("Recipe:", "Site folder:") and before a list or a quoted value. A message rarely needs more than one colon.
2. **Repeated reassurance.** State a safety fact once, where it matters: the release dialog, the dry-run notice, the server key check, and a refused or failed action that could otherwise worry someone. Don't repeat it in every help line on the same screen.
3. **A reason attached to everything.** Drop the ", so …" or "(…)" explanation unless the reader needs it to decide or act.
4. **Literary or British idioms.** "carry on", "put right", "whoever looks after email", "go on", "as it is now". Use plain US equivalents: "continue", "fix", "your email administrator".
5. **Rule of three and padding lists.** Don't list three things where one or two make the point.
6. **Negative parallelism.** Avoid "not X but Y", "X, never Y" and "only X, not Y" unless the contrast is the point, as with a preview that never sends.
7. **Explaining the app's own mechanics.** Readers don't need to know about jobs, errands or the site being read again. Say what they see or what to do.
8. **Filler.** "still", "here", "at once", "as it is", "for you" and "now", where they add nothing.
9. **Stacked asides.** At most one aside per sentence. Prefer a period to parentheses or a dash. En dashes ( – ) are the only dash; never use an em dash.
10. **Inconsistent terms.** Use the app's words throughout: site, site folder, feed, recipe, vendor, run, preview, build only, dry run, delivery, server key, fingerprint, FOLIO, Credential Manager.

## Good patterns

- Lead with what happened or what to do: "Preview ready." "Couldn't log in to FOLIO. Check the password and try again."
- Say what's wrong, then the fix: "Lakeside isn't empty. Choose an empty folder or another name."
- Use numbers and names, not vague words: "3 bibs, 6 items", not "some records".
- Give buttons action verbs that match what they do. Keep "…" on a button that opens a dialog or needs another step.

## Examples

| Before | After |
|---|---|
| Not confirmed yet: nothing logs in or sends until you enter the vendor's fingerprint under Change delivery… on this tab. | Not confirmed yet. Enter the vendor's fingerprint under Change delivery… before the feed can connect. |
| Not yet. You can carry on: the feed is saved paused, but its runs fail until this is put right. | Not yet. You can continue, but the feed's runs will fail until this is fixed. |
| Your mail server's name, from whoever looks after email. | Your mail server's name. Your email administrator can tell you. |
| The feed is saved paused, so it does not run on this schedule until you resume it. | The feed starts paused. It runs on this schedule once you resume it. |
| Nothing waits for you: no run is held, blocked or failed. | Nothing needs you right now. |
| No result yet. Preview builds a sample from live data. Run updates from FOLIO, builds the files and sends them, or holds them for you to review. | No results yet. Preview builds sample files from FOLIO. Run builds the files and sends them, or holds them for your review. |
| Closing now would cut it off half way, so the window can close once it finishes instead. | Closing now would interrupt it. The window can close when it finishes instead. |

## Rules that must hold

- **Names of controls.** When text names a button, tab, menu item or view ("under Change delivery…", "use Review and send…", "the Summary tab"), the name matches that control's label exactly. Renaming a control means updating every string that names it, including in `model.py`.
- **Label in name** (WCAG 2.5.3). Every button's accessible name contains its visible label, without the `&` and `…`, so voice control can find it by what's on screen. A test checks every button in the window and its dialogs.
- **Mnemonics.** Within a view or dialog, `&` letters are unique, and no main-window button uses the Site menu's `S`. Tests check both.
- **Accessibility.** An accessible name says what the control is and is unique among its siblings. An announced message makes sense when heard alone.
- **Plain text.** Labels set as plain text get no markup. The few HTML strings (`<p>`, `<ul>`, `<b>`) keep their markup and escaping.
- **Format strings.** Keep every `{placeholder}`, `%s` and plural rule. Check how the sentence reads with real values filled in: feed `chq`, a long title such as "collectionHQ Academic – bibs and items", and counts of 0, 1 and many.
- **Secrets.** Text never shows a password or asks for one where the code didn't before. Wording about Credential Manager stays accurate, including on a computer without it.
- **Safety facts** that must stay somewhere on the screens where they apply (reworded is fine):
  - a preview or build never sends;
  - a new feed is paused, and a dry run until it has a delivery;
  - nothing logs in to an SFTP server until its key is confirmed;
  - the release dialog shows exactly what will be sent;
  - a refused action changed nothing;
  - the Windows password is typed only into the console window, never into the app.
- **Tests.** Many tests assert exact text. Update them with the text, and keep what each one checks: never weaken, drop or blur an assertion. Where a test checks a safety fact, it must still find it.
- **Design notes.** Where `docs/design.md` quotes an app string, update the quote.
