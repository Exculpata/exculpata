Not legal advice. Verify the rules in your jurisdiction.

# Lawyer and operator vetting checklist

Complete this privately for each proposed use and actual installation. This kit
organizes evidence and unanswered questions; it does not establish ethics
compliance, privilege, a service contract or permission to disclose information.
Do not place a completed client-specific checklist in this public repository.

Record `[reviewer]`, `[jurisdiction and court]`, `[matter reference]`, `[date]`,
`[source commit/images/model revisions]`, `[operator and providers]`,
`[proposed tasks/data categories]`, `[evidence location]` and `[next review date]`.
Use **verified / unresolved / not applicable with reason**, an owner and a due
date for each item. Unresolved material risks require a decision before use.

## Primary-source map

Checked October 15, 2026. Page references below are printed pages unless stated
otherwise. These materials interpret professional duties; their status and
applicability differ. Consult current jurisdictional rules, client instructions,
protective orders and court requirements. No row is a universal consent mandate.

| Source and status | Relevant guidance to assess | Exculpata evidence and remaining decision |
| --- | --- | --- |
| [ABA Formal Opinion 512][aba], July 29, 2024; ABA Model Rules interpretation | Pages 6–8 distinguish confidentiality risk, informed consent for the described self-learning uses with disclosure risk, and fact-specific client communication. Generic engagement permission is insufficient for that consent. Page 11 addresses vendor diligence. | [Local inference and its limits](DATA_HANDLING.md#where-information-goes) address the technical training question; they do not decide disclosure duties. Verify actual recipients, internal access, terms and risks; explain the specific proposed use and alternatives. |
| [California Practical Guidance, 2026 revision][ca]; practical guidance | Pages 5–7 address actual collection, use, storage, disclosure, security and retention, beyond marketing claims; material confidentiality/security risks call for informed consent. Agent access and external transmissions need safeguards and oversight. Communication and client restrictions remain relevant. | [Network inventory](DATA_HANDLING.md#network-inventory-and-operator-choices) and [security model](../SECURITY_MODEL.md) supply starting facts. Verify permissions and scope, supervise workflows, and reassess when models, tasks or access change. |
| [Florida Opinion 24-1][fl], January 19, 2024; expressly nonbinding | “Confidentiality” applies Opinion 12-3's cloud diligence: enforceable confidentiality/security, breach/process notice, reputation, liability limits, retention after termination and proprietary claims. It recommends informed consent when third-party AI use discloses confidential information; it distinguishes use without such disclosure. | [Reference topology and copies](DATA_HANDLING.md) identify which providers need review. Obtain actual operator/provider commitments; the code and [license](../../LICENSE) do not supply a negotiated confidentiality, notice, retention or data-rights agreement. |
| [Texas Opinion 705][tx], February 2025; committee ethics opinion | Pages 2–4 build on Opinion 680: understand technology, terms and security; train staff; assess self-learning and disclosure. If not reasonably satisfied confidentiality is protected, at minimum consult and obtain consent before inputting confidential information. Protected necessary service providers are addressed separately. | [Inference evidence and endpoint limits](DATA_HANDLING.md#where-information-goes) inform, but cannot resolve, the confidentiality assessment. Record vendor review, client-specific risk analysis and human verification responsibilities. |
| [NYC Bar Formal Opinion 2024-5][nyc], August 7, 2024; committee guidance on New York rules | Pages 2–3 address informed consent for confidential inputs shared with third parties or inadequately protected use, internal ethical screens even in closed systems, changing terms, training and retention. Consent requires understanding consequences. Pages 6–7 treat communication as fact-specific; footnote 5 cites NYSBA 1020. | [Access and retention limits](DATA_HANDLING.md#retention-access-and-incident-responsibilities) and [network inventory](DATA_HANDLING.md#network-inventory-and-operator-choices) support review of the actual system. Determine whether disclosure and/or consent is required; do not equate local deployment with an ethical screen. |
| [Ohio Advisory Opinion 2017-05][oh-cloud], June 9, 2017; nonbinding cloud/virtual-office opinion | Printed pages 3–4 (PDF pages 4–5) address safeguards, confidentiality/security agreements, maintenance/backups, notice of subpoenas, unauthorized access and breaches, and retrieval after termination or provider failure. It is not an AI-training opinion. | [Storage/backup contract](../STORAGE_AND_BACKUP.md) and [closure contract](../CASE_LIFECYCLE.md) identify copies and recovery work. Require actual provider commitments and a restore/retrieval drill; do not infer a deletion deadline from the opinion. |
| [Ohio Ethics Guide on AI, April 2026][oh-ai]; nonbinding Board staff guidance, not Board/Commissioner/Supreme Court views | Printed pages 3–4 address sharing, stored-data training, security, terms and proprietary claims, safeguards and possible consent. Pages 7–8 address vendor supervision. Recommendations about storage/training/sharing are not a binding ban on local storage; research alone does not automatically require disclosure. | [Inference, retained work and exceptions](DATA_HANDLING.md) must be reviewed together. Assess the actual configuration and contemplated use; distinguish advisory best practices from binding duties. |

The [State Bar's current resources page][ca-status] records May 14, 2026 approval;
the nine-page revision expressly replaces the 2023 guidance. The issue's
[older California PDF][ca-old] is historical, not the current guidance. Ohio's
[official resource library][oh-library] links the AI guide; the actual 2017
opinion is linked above instead of the news summary.

## Complete the operational assessment

| Topic | Evidence to inspect | Questions and required record |
| --- | --- | --- |
| Confidentiality and vendor vetting | [Data paths, allowlists and network exceptions](DATA_HANDLING.md); ABA pp. 6–7, 11; CA pp. 5–6; FL “Confidentiality”; TX pp. 2–4; NYC pp. 2–3; both Ohio sources above. | Who can receive content, metadata, prompts, backups or exports? Who administers the host? Are security duties enforceable? Record subprocessors, access controls, internal ethical screens, reputation, security testing and liability limits. |
| Retention and retrieval | [Lifecycle](../CASE_LIFECYCLE.md), [backups](../STORAGE_AND_BACKUP.md); FL cloud diligence, NYC p. 3, Ohio 2017 pp. 3–4. | Set matter retention/holds, queue expiry, archive/backup disposal and retrieval at termination. Verify recovery. App purge is not a promise of physical erasure or deletion of external copies. None of these sources supplies a universal number of retention days. |
| Data rights | [License](../../LICENSE), [model acquisition terms](../MODEL_ACQUISITION.md); FL proprietary-rights inquiry and Ohio AI pp. 3–4. | Identify ownership/use rights asserted by every provider, licenses, access on termination and rights to export. An open-source software license does not establish ownership of client data or rights to all model outputs. |
| Training and self-learning | [Inference evidence](DATA_HANDLING.md#where-information-goes); ABA pp. 6–7, CA pp. 5–6, FL, TX p. 3, NYC p. 3 and Ohio AI pp. 3–4. | Record whether any configured service stores, reuses, trains on or shares inputs, and whether policy can change. Separate retained case work from weight training; verify technical controls and provider terms. |
| Breach and compelled-disclosure notice | [Security reporting](../../SECURITY.md); FL cloud diligence and Ohio 2017 pp. 3–4. | Name the incident owner and escalation channel. Agree notice triggers/timing, subpoena handling, preservation and cooperation with each provider. Repository reporting instructions do not provide those commitments; these opinions do not set a universal notice deadline. |
| Disclosure and informed consent | [Fill-in template](CLIENT_DISCLOSURE_TEMPLATE.md); source-specific distinctions above. | Record why disclosure and/or consent is needed, what risks and alternatives were explained, questions answered and restrictions accepted. Revisit material changes. A signature does not cure an unsafe workflow or waive professional duties. |
| Competence and human review | [Security limitations](../SECURITY_MODEL.md), [beta qualification limits](../BETA_RELEASE_NOTES.md); ABA pp. 2–4, CA pp. 4–5, FL work-product discussion, TX pp. 4–5, NYC competence guidance. | Identify reviewers, representative testing, output/source checks, omitted-source handling, training and escalation. Decide fees and any tribunal disclosure under applicable requirements separately. |

## Verification record and limits

The companion [data-handling proof](DATA_HANDLING.md#bounded-network-evidence)
identifies reproducible configuration/unit checks and their limits. Keep local
results, the rendered Compose graph, actual destination review and any synthetic
traffic capture with the private installation assessment. Public PR evidence must
contain only synthetic results and public source links.

Primary text was checked through source retrieval and an independent primary-source
review on October 15, 2026. Automated HTTP results and any retrieval exceptions
are recorded in the implementing PR, not silently treated as passing links.
Availability is time- and client-dependent; fetch the current primary text again
before relying on this checklist. Legal-source verification is separate from
technical acceptance and does not qualify a confidential workload.

[aba]: https://www.americanbar.org/content/dam/aba/administrative/professional_responsibility/ethics-opinions/aba-formal-opinion-512.pdf
[ca]: https://www.calbar.ca.gov/sites/default/files/portals/0/documents/ethics/Generative-AI-Practical-Guidance.pdf
[ca-status]: https://www.calbar.ca.gov/legal-professionals/ethics-compliance-practice-resources/ethics/ethics-technology-resources
[ca-old]: https://www.calbar.ca.gov/sites/default/files/2025-09/Generative-AI-Practical-Guidance.pdf
[fl]: https://www.floridabar.org/etopinions/opinion-24-1/
[tx]: https://tcle-web.s3.amazonaws.com/public/documents/Opinion_705.pdf
[nyc]: https://www.nycbar.org/wp-content/uploads/2024/08/20221329_GenerativeAILawPractice.pdf
[oh-cloud]: https://www.ohioadvop.org/wp-content/uploads/2017/03/Adv.-Op.-2017-5.pdf
[oh-ai]: https://www.bpc.ohio.gov/_files/ugd/c6a571_4e12e5f9a0104eaebe9351007c899b4f.pdf
[oh-library]: https://www.supremecourt.ohio.gov/courts/services-to-courts/artificial-intelligence-resource-library/
