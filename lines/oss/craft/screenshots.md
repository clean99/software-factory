# Screenshots and demos

For a visible change expected by the template or maintainers, run the real local app/example/storybook before and after the fix. Keep viewport, input and relevant environment comparable. Record commands, source commits and paths under the item's demo/. Explain infeasibility in validation.md rather than fabricating proof.

A unit assertion is not browser, layout or accessibility evidence. Use a real browser when the contract needs an engine. Pair the image with the specific behavioral assertion; an attractive screenshot does not prove absence of regressions.

Workers only prepare local files and a concise index. Local paths do not render on GitHub. The coordinator runs `bin/factory strip-image <files...>`, then must visually inspect the cleaned files, including colors and private pixels. Only those cleaned, inspected bytes may enter the media repository. Record byte-bound binary review entries, run `bin/factory scan-repo <media-repo> --allow-file <review.json>`, and publish only after it passes through the coordinator's scanned push wrapper. Put the resulting public URLs in pr.md and recheck the body. Never upload through a browser or send an unstripped file anywhere; visual review cannot waive metadata. Match extension to content (.jpg for JPEG).
