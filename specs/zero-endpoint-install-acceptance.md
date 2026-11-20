# Check a fresh node through its HTTPS gateway

## Kickoff

A team with zero configured models can sign in to a fresh node, import and review sources, apply review codes, and follow exact-search results to their source. The release check must exercise the real installer and its locally authenticated gateway with PostgreSQL and an up-to-date antivirus scanner.

Add an opt-in hosted workflow, a synthetic acceptance driver, and a short operating note. Run the documented evaluation installer on an empty root, run the node doctor, and use ordinary HTTPS requests to sign in, upload an antivirus-scanned CSV and the packaged OCR PDF, follow the cited source and page, verify original bytes, and persist a review code. Report only check results, with the temporary account password and generated node state confined to the disposable runner. The workflow can be repeated for a release candidate or the resulting main branch.

[Operator-configured model APIs, issue 558](https://github.com/Exculpata/exculpata/issues/558)

## Kickoff for final release verification

On a CPU host, verify that the final release can retire a saved review bundle while retaining local sign-in, both source originals and their citations, and a reviewer-applied code. Use an immutable product checkout, a separate released installer to generate the historical settings, and the verification driver on this branch. The expected changes are limited to the hosted verification workflow and its driver. Acceptance requires the staged release digest to match the checked-out product, healthy CPU services before and after cutover, a restarted application and the retained data and review assertions. [Model endpoint migration](https://github.com/Exculpata/exculpata/issues/558)
