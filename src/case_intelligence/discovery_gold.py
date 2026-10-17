"""Versioned generated discovery-quality gold for Exculpata model evaluation.

The suite is deliberately synthetic. It represents difficult review language
patterns without containing case, client, employee, or other personal data.
"""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)

import hashlib
import json
from dataclasses import asdict, dataclass


GOLD_SUITE_ID = f"{LEGACY_STEM}-discovery-gold-v1"
GOLD_SCHEMA_VERSION = 1
DEFAULT_DOCUMENT_COUNT = 1_200


@dataclass(frozen=True)
class GoldDocument:
    document_id: str
    source_name: str
    media_type: str
    text: str
    line_start: int | None = None
    line_end: int | None = None


@dataclass(frozen=True)
class RetrievalCase:
    case_id: str
    category: str
    question: str
    expected_documents: tuple[str, ...]
    answer_sample: bool = False
    answerable: bool = True
    required_concepts: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class ReviewCriterion:
    criterion_id: str
    criterion: str
    include_guidance: str
    exclude_guidance: str


@dataclass(frozen=True)
class ClassificationCase:
    case_id: str
    criterion_id: str
    source_name: str
    text: str
    expected: str
    challenge: str


def document_id(ordinal: int) -> str:
    return f"gold-record-{ordinal:04d}"


_TARGET_DOCUMENTS: dict[int, tuple[str, str, str, int | None, int | None]] = {
    7: (
        "Property intake VX-1047.pdf",
        "application/pdf",
        "Reference VX-1047 identifies a blue canvas bag logged in the east storage room at 08:15.",
        None,
        None,
    ),
    89: (
        "Property intake QZ-8821.pdf",
        "application/pdf",
        "Reference QZ-8821 identifies a silver key ring logged at the north service desk at 14:20.",
        None,
        None,
    ),
    143: (
        "Cedar Station recovery note.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "A crimson bicycle was recovered beside Cedar Station. The storage tag was RED-77.",
        None,
        None,
    ),
    202: (
        "Disbursement approval AE-19.eml",
        "message/rfc822",
        "Supervisor Dana Cross gave final approval on October 3, 2026 to pay invoice AE-19 in the amount of $18,450.",
        None,
        None,
    ),
    278: (
        "Generated interview note.pdf",
        "application/pdf",
        "The cellular handset lost power before the scheduled meeting and therefore could not place another call.",
        None,
        None,
    ),
    320: ("Lantern checkpoint 01.txt", "text/plain", "Operation Lantern checkpoint one reported marker LANTERN-01 and an all-clear status.", None, None),
    321: ("Lantern checkpoint 02.txt", "text/plain", "Operation Lantern checkpoint two reported marker LANTERN-02 and a delayed opening.", None, None),
    322: ("Lantern checkpoint 03.txt", "text/plain", "Operation Lantern checkpoint three reported marker LANTERN-03 and an all-clear status.", None, None),
    323: ("Lantern checkpoint 04.txt", "text/plain", "Operation Lantern checkpoint four reported marker LANTERN-04 and a radio outage.", None, None),
    324: ("Lantern checkpoint 05.txt", "text/plain", "Operation Lantern checkpoint five reported marker LANTERN-05 and an all-clear status.", None, None),
    325: ("Lantern checkpoint 06.txt", "text/plain", "Operation Lantern checkpoint six reported marker LANTERN-06 and a 22:10 closure.", None, None),
    350: (
        "October meeting minutes.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "The planning meeting was moved from October 5 to October 7, 2026. The minutes confirm the meeting occurred on October 7 at 09:30.",
        None,
        None,
    ),
    360: (
        "First witness account.pdf",
        "application/pdf",
        "Witness A reported that the courier entered through the north entrance at approximately 17:05.",
        None,
        None,
    ),
    361: (
        "Second witness account.pdf",
        "application/pdf",
        "Witness B reported that the same courier used the south entrance shortly after 17:00, contrary to the first account.",
        None,
        None,
    ),
    370: (
        "Warehouse badge audit.csv",
        "text/csv",
        "The independent badge system recorded Avery Lark entering the warehouse at 08:12.",
        None,
        None,
    ),
    371: (
        "Warehouse camera index.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "The camera index independently shows Avery Lark inside the warehouse lobby at 08:14.",
        None,
        None,
    ),
    411: (
        "Avery Lark interview.pdf",
        "application/pdf",
        "Avery Lark reported that the delivery van arrived at the west gate at 6:40 p.m. and departed eleven minutes later.",
        None,
        None,
    ),
    420: (
        "Pier interview.mp3",
        "audio/mpeg",
        "Speaker 2: I left the steel toolbox under the pier before sunrise. Speaker 1: You mean below the east walkway? Speaker 2: Yes, under that pier walkway.",
        342_000,
        366_000,
    ),
    430: (
        "Request to district counsel.eml",
        "message/rfc822",
        "To: District Counsel Mira Chen. Erin Vale wrote: Please advise whether the revised affidavit establishes probable cause for the requested search warrant.",
        None,
        None,
    ),
    440: (
        "Invoice authorization register.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "Invoice AE-19 | amount $18,450 | final approval Dana Cross | approval date 2026-10-03 | authorization DC-441.",
        None,
        None,
    ),
    450: (
        "Corrected vehicle report.pdf",
        "application/pdf",
        "Correction: the vehicle was not red. After reviewing the daylight image, the final recorded color is dark gray.",
        None,
        None,
    ),
    460: (
        "Extension notice.eml",
        "message/rfc822",
        "The original January 15 deadline was extended by written agreement. The revised production deadline is January 22, 2027.",
        None,
        None,
    ),
    470: (
        "Warehouse staffing directory.csv",
        "text/csv",
        "Robert Keene | preferred name Bobby | role warehouse manager | shift morning.",
        None,
        None,
    ),
    471: (
        "Bobby Keene interview.pdf",
        "application/pdf",
        "Bobby Keene stated that he supervised the warehouse receiving team. The personnel directory identifies Bobby as Robert Keene.",
        None,
        None,
    ),
    480: (
        "NOVA-31 collection log.pdf",
        "application/pdf",
        "Technician Rowan Vale sealed Sample NOVA-31 at 10:02 and initialed the collection label.",
        None,
        None,
    ),
    481: (
        "NOVA-31 transfer receipt.pdf",
        "application/pdf",
        "Courier Jalen Orr accepted sealed Sample NOVA-31 from Rowan Vale at 10:18 for transfer.",
        None,
        None,
    ),
    482: (
        "NOVA-31 cold storage log.csv",
        "text/csv",
        "Sample NOVA-31 was received into cold storage locker C-14 at 10:41 by technician Imani Frost.",
        None,
        None,
    ),
    577: (
        "Meridian Transit receipt.pdf",
        "application/pdf",
        "The Meridian Transit receipt lists platform four, coach 18, and a departure at 07:32 on September 12, 2026.",
        None,
        None,
    ),
    644: (
        "NOVA-31 laboratory note.pdf",
        "application/pdf",
        "The laboratory note states that Sample NOVA-31 was sealed by technician Rowan Vale and transferred to cold storage.",
        None,
        None,
    ),
    731: (
        "Camera maintenance log.pdf",
        "application/pdf",
        "The surveillance camera was unavailable for twelve minutes beginning at 21:06 while a replacement power supply was installed.",
        None,
        None,
    ),
    844: (
        "Library loading dock statement.pdf",
        "application/pdf",
        "A witness account places the orange utility truck behind the library loading dock shortly after noon.",
        None,
        None,
    ),
    963: (
        "Kestrel dispatch note.pdf",
        "application/pdf",
        "Kestrel Team cancelled the river checkpoint after the weather warning was received.",
        None,
        None,
    ),
}


def retrieval_documents(count: int = DEFAULT_DOCUMENT_COUNT) -> tuple[GoldDocument, ...]:
    if not 1_000 <= count <= 10_000:
        raise ValueError("discovery gold requires between 1,000 and 10,000 documents")
    agencies = ("North Unit", "South Unit", "Central Unit", "Harbor Unit")
    streets = ("Ash Street", "Birch Avenue", "Cedar Road", "Dover Lane")
    document_types = (
        ("pdf", "application/pdf"),
        ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("txt", "text/plain"),
        ("eml", "message/rfc822"),
        ("csv", "text/csv"),
    )
    result: list[GoldDocument] = []
    for ordinal in range(count):
        suffix, media_type = document_types[ordinal % len(document_types)]
        source_name = f"Generated routine record {ordinal:04d}.{suffix}"
        text = (
            f"Generated administrative record {ordinal:04d}. "
            f"The {agencies[ordinal % len(agencies)]} logged a routine status update near "
            f"{streets[ordinal % len(streets)]}. The entry concerns ordinary inventory "
            "rotation and contains no case, client, employee, or personal information."
        )
        line_start = line_end = None
        target = _TARGET_DOCUMENTS.get(ordinal)
        if target is not None:
            source_name, media_type, target_text, line_start, line_end = target
            text = f"{text} {target_text}"
        result.append(
            GoldDocument(
                document_id(ordinal),
                source_name,
                media_type,
                text,
                line_start,
                line_end,
            )
        )
    return tuple(result)


def retrieval_cases() -> tuple[RetrievalCase, ...]:
    doc = document_id
    return (
        RetrievalCase("exact-reference-vx", "exact_identifier", "What was logged under reference VX-1047?", (doc(7),), True, True, (("blue canvas bag",), ("east storage room",), ("08:15",))),
        RetrievalCase("exact-reference-qz", "exact_identifier", "What does property reference QZ-8821 identify?", (doc(89),)),
        RetrievalCase("semantic-bicycle", "semantic_alias", "Where was the red bike found and what tag was assigned?", (doc(143),), True, True, (("Cedar Station",), ("RED-77",))),
        RetrievalCase("semantic-phone", "causal", "Why was the mobile phone unable to make another call?", (doc(278),), True, True, (("lost power", "lost its power"),)),
        RetrievalCase("person-vehicle-time", "name_time", "What did Avery Lark report about the delivery van's timing?", (doc(411),), True, True, (("6:40",), ("eleven minutes", "11 minutes"))),
        RetrievalCase("transit-itinerary", "name_time", "When and where did the Meridian Transit trip depart?", (doc(577),), True, True, (("platform four", "platform 4"), ("coach 18",), ("07:32",), ("September 12", "Sep. 12"))),
        RetrievalCase("sample-seal", "chain_of_custody", "Who sealed NOVA-31 and where was it sent?", (doc(644),)),
        RetrievalCase("camera-outage", "causal", "How long was the surveillance camera unavailable, when did it begin, and why?", (doc(731),), True, True, (("twelve minutes", "12 minutes"), ("21:06",), ("power supply",))),
        RetrievalCase("truck-location", "semantic_alias", "Where was the orange service vehicle observed?", (doc(844),)),
        RetrievalCase("checkpoint-cancellation", "causal", "Why did Kestrel Team call off the river checkpoint?", (doc(963),)),
        RetrievalCase("lantern-multi-source", "multi_source", "Which records describe the six Operation Lantern checkpoints?", tuple(doc(value) for value in range(320, 326))),
        RetrievalCase("meeting-actual-date", "temporal_correction", "On what date did the planning meeting actually occur after it was moved?", (doc(350),), True, True, (("October 7", "Oct. 7"), ("09:30",))),
        RetrievalCase("competing-entrances", "conflicting_accounts", "What competing accounts describe which entrance the courier used?", (doc(360), doc(361)), True, True, (("north entrance",), ("south entrance",))),
        RetrievalCase("independent-corroboration", "corroboration", "What independent system records place Avery Lark at the warehouse around 8:15?", (doc(370), doc(371)), True, True, (("badge",), ("camera",), ("08:12",), ("08:14",))),
        RetrievalCase("transcript-speaker-location", "media_transcript", "According to Speaker 2, where was the steel toolbox left?", (doc(420),), True, True, (("Speaker 2",), ("under the pier", "pier walkway"))),
        RetrievalCase("counsel-request", "email", "What did Erin Vale ask district counsel about the search warrant?", (doc(430),), True, True, (("probable cause",), ("search warrant",))),
        RetrievalCase("invoice-approval", "spreadsheet", "Who finally approved invoice AE-19, for how much, and when?", (doc(202), doc(440)), True, True, (("Dana Cross",), ("18,450",), ("October 3", "2026-10-03"))),
        RetrievalCase("corrected-color", "negation_correction", "What was the vehicle's final recorded color after correction?", (doc(450),), True, True, (("dark gray",),)),
        RetrievalCase("revised-deadline", "temporal_correction", "What is the revised production deadline?", (doc(460),), True, True, (("January 22",), ("2027",))),
        RetrievalCase("person-alias-role", "entity_alias", "Who is Bobby Keene and what role did he hold?", (doc(470), doc(471)), True, True, (("Robert Keene",), ("warehouse manager",))),
        RetrievalCase("custody-sequence", "chain_of_custody", "Trace NOVA-31 from sealing through cold storage.", (doc(480), doc(481), doc(482)), True, True, (("Rowan Vale",), ("Jalen Orr",), ("cold storage",), ("Imani Frost",))),
        RetrievalCase("invoice-cross-format", "cross_format", "Find the email and spreadsheet that record final approval of AE-19.", (doc(202), doc(440))),
        RetrievalCase("nova-cross-record", "cross_format", "Which records document collection, courier transfer, and cold storage for NOVA-31?", (doc(480), doc(481), doc(482))),
        RetrievalCase("avery-cross-record", "cross_format", "Which independent records corroborate Avery Lark's presence inside the warehouse?", (doc(370), doc(371))),
        RetrievalCase("absent-purple-helicopter", "abstention", "Who piloted the purple helicopter identified as ORBIT-99?", (), True, False, ()),
    )


def review_criteria() -> tuple[ReviewCriterion, ...]:
    return (
        ReviewCriterion("actual-blue-arrival", "Include only if the source expressly states that a blue motor vehicle actually arrived at the west gate.", "Require blue, a motor vehicle, a completed arrival, and the west gate.", "Exclude plans, questions, cancellations, departures, negations, other colors, bicycles, or another gate."),
        ReviewCriterion("final-payment-approval", "Include only if the source expressly records final approval by a named supervisor for a payment greater than $10,000.", "Require a completed authorization, a named supervisor, and an amount over $10,000.", "Exclude requests, proposals, denials, revoked approval, missing approver names, and amounts of $10,000 or less."),
        ReviewCriterion("nova-transfer", "Include only if the source expressly states that Rowan Vale transferred sealed Sample NOVA-31 to cold storage.", "Require Rowan Vale, NOVA-31, sealed condition, an actual transfer, and cold storage.", "Exclude planned or denied transfers, another sample, another person, unsealed material, or another destination."),
        ReviewCriterion("october-seven-meeting", "Include only if the source expressly confirms that the planning meeting actually occurred on October 7, 2026.", "Require a completed meeting and the exact date October 7, 2026.", "Exclude scheduling alone, cancellations, questions, other dates, or an event other than the planning meeting."),
        ReviewCriterion("speaker-two-toolbox", "Include only if the transcript expressly attributes to Speaker 2 a statement that the steel toolbox was left under the pier.", "Treat the transcript as evidence of what was said, not proof the event occurred. Require Speaker 2, steel toolbox, and under the pier.", "Exclude another speaker, questions without an answer, denial, inaudible text, another object, or another location."),
        ReviewCriterion("counsel-warrant-advice", "Include only if the communication expressly asks district counsel for legal advice about whether a search-warrant affidavit establishes probable cause.", "Require a request directed to district counsel, legal advice, a search-warrant affidavit, and probable cause.", "Exclude scheduling, non-counsel recipients, arrest warrants, statements that no advice is requested, or mere mention of a search warrant."),
        ReviewCriterion("final-dark-gray-correction", "Include only if the source expressly states that the final corrected vehicle color is dark gray rather than red.", "Require a vehicle-color correction, dark gray as the final color, and rejection of red.", "Exclude an initial red description, uncertainty, other colors, unrelated dark gray objects, or no correction."),
        ReviewCriterion("avery-system-corroboration", "Include only if an independent badge or camera system expressly places Avery Lark inside the warehouse between 08:10 and 08:20.", "Require Avery Lark, an independent badge or camera record, the warehouse, and a time from 08:10 through 08:20.", "Exclude self-reports, plans, other people, other locations, times outside the window, or an unavailable system."),
    )


_CLASSIFICATION_ROWS: dict[str, tuple[tuple[str, str, str, str], ...]] = {
    "actual-blue-arrival": (
        ("direct", "include", "The blue sedan arrived at the west gate at 18:40.", "direct"),
        ("witness", "include", "The witness saw a blue delivery van arrive through the west gate.", "reported_explicit"),
        ("split", "include", "The arriving vehicle was blue. The entry point was the west gate.", "split_facts"),
        ("passive", "include", "Arrival of the blue truck at the west gate was recorded by camera 4.", "passive_voice"),
        ("negated", "not_identified", "The blue vehicle did not arrive at the west gate; the first note was incorrect.", "negation"),
        ("expected", "not_identified", "A blue vehicle was expected to arrive at the west gate later.", "future_plan"),
        ("cancelled", "not_identified", "The planned west-gate arrival of the blue vehicle was cancelled.", "cancelled"),
        ("wrong-color", "not_identified", "A red vehicle arrived at the west gate.", "wrong_attribute"),
        ("wrong-gate", "not_identified", "A blue vehicle arrived at the east gate.", "wrong_location"),
        ("bicycle", "not_identified", "A blue bicycle arrived at the west gate with its rider.", "wrong_object"),
        ("departure", "not_identified", "The blue van departed from the west gate at 18:40.", "wrong_event"),
        ("question", "not_identified", "The reviewer asked whether a blue vehicle arrived at the west gate; the source gives no answer.", "question_only"),
    ),
    "final-payment-approval": (
        ("direct", "include", "Supervisor Dana Cross gave final approval to pay $18,450.", "direct"),
        ("authorized", "include", "Supervisor Malik Stone authorized the completed $12,200 disbursement.", "semantic_equivalent"),
        ("register", "include", "Payment $31,900 | final approval | supervisor Imani Frost.", "structured_row"),
        ("passive", "include", "Final payment of $10,001 was approved by supervisor Erin Vale.", "boundary"),
        ("requested", "not_identified", "Staff requested supervisor Dana Cross approve payment of $18,450.", "request_only"),
        ("proposed", "not_identified", "A proposed payment of $22,000 awaits supervisor review.", "proposal"),
        ("denied", "not_identified", "Supervisor Dana Cross denied the $18,450 payment.", "denial"),
        ("revoked", "not_identified", "Supervisor Dana Cross initially approved $18,450 but the final approval was revoked.", "correction"),
        ("threshold", "not_identified", "Supervisor Erin Vale gave final approval to pay exactly $10,000.", "threshold"),
        ("unnamed", "not_identified", "Final approval was recorded for a payment of $18,450; no supervisor is identified.", "missing_entity"),
        ("wrong-role", "not_identified", "Clerk Dana Cross gave final approval to pay $18,450.", "wrong_role"),
        ("question", "not_identified", "Did a supervisor finally approve the $18,450 payment? The register is blank.", "question_only"),
    ),
    "nova-transfer": (
        ("direct", "include", "Rowan Vale transferred sealed Sample NOVA-31 to cold storage.", "direct"),
        ("receipt", "include", "Cold storage received sealed NOVA-31 from Rowan Vale at 10:41.", "passive_voice"),
        ("split", "include", "Sample NOVA-31 remained sealed. Rowan Vale completed its transfer into cold storage.", "split_facts"),
        ("register", "include", "NOVA-31 | sealed | transfer Rowan Vale | destination cold storage | completed.", "structured_row"),
        ("planned", "not_identified", "Rowan Vale planned to transfer sealed NOVA-31 to cold storage.", "future_plan"),
        ("denied", "not_identified", "Rowan Vale did not transfer sealed NOVA-31 to cold storage.", "negation"),
        ("other-sample", "not_identified", "Rowan Vale transferred sealed Sample NOVA-13 to cold storage.", "wrong_identifier"),
        ("other-person", "not_identified", "Jalen Orr transferred sealed Sample NOVA-31 to cold storage.", "wrong_entity"),
        ("other-place", "not_identified", "Rowan Vale transferred sealed Sample NOVA-31 to the evidence desk.", "wrong_location"),
        ("unsealed", "not_identified", "Rowan Vale transferred unsealed Sample NOVA-31 to cold storage.", "wrong_condition"),
        ("sealing-only", "not_identified", "Rowan Vale sealed Sample NOVA-31 beside the cold-storage room.", "missing_event"),
        ("question", "not_identified", "The form asks whether Rowan Vale transferred sealed NOVA-31 to cold storage; no response appears.", "question_only"),
    ),
    "october-seven-meeting": (
        ("minutes", "include", "The planning meeting occurred on October 7, 2026.", "direct"),
        ("held", "include", "Minutes confirm the planning meeting was held October 7, 2026 at 09:30.", "semantic_equivalent"),
        ("rescheduled-held", "include", "Moved from October 5, the planning meeting ultimately took place on October 7, 2026.", "temporal_correction"),
        ("attendance", "include", "Attendance for the October 7, 2026 planning meeting was recorded after the meeting concluded.", "completed_event"),
        ("scheduled", "not_identified", "The planning meeting is scheduled for October 7, 2026.", "schedule_only"),
        ("cancelled", "not_identified", "The October 7, 2026 planning meeting was cancelled.", "cancelled"),
        ("wrong-date", "not_identified", "The planning meeting occurred on October 8, 2026.", "wrong_date"),
        ("wrong-year", "not_identified", "The planning meeting occurred on October 7, 2025.", "wrong_year"),
        ("tentative", "not_identified", "October 7, 2026 is a tentative date for the planning meeting.", "uncertainty"),
        ("other-meeting", "not_identified", "The budget meeting occurred on October 7, 2026.", "wrong_event"),
        ("question", "not_identified", "Did the planning meeting occur on October 7, 2026? The minutes are missing.", "question_only"),
        ("denial", "not_identified", "Contrary to the calendar, no planning meeting occurred on October 7, 2026.", "negation"),
    ),
    "speaker-two-toolbox": (
        ("direct", "include", "Speaker 2: I left the steel toolbox under the pier.", "direct_quote"),
        ("answer", "include", "Speaker 1: Where was it? Speaker 2: The steel toolbox was under the pier.", "question_answer"),
        ("pronoun", "include", "Speaker 2: The steel toolbox? I put it under the pier before sunrise.", "pronoun_resolution"),
        ("confirmation", "include", "Speaker 1: Under the pier? Speaker 2: Yes, that is where I left the steel toolbox.", "confirmation"),
        ("speaker-one", "not_identified", "Speaker 1: I left the steel toolbox under the pier. Speaker 2: I do not know.", "wrong_speaker"),
        ("question-only", "not_identified", "Speaker 2: Was the steel toolbox left under the pier? Speaker 1: No answer.", "question_only"),
        ("denial", "not_identified", "Speaker 2: I did not leave the steel toolbox under the pier.", "negation"),
        ("inaudible", "not_identified", "Speaker 2: The steel toolbox was [inaudible].", "missing_location"),
        ("wrong-object", "not_identified", "Speaker 2: I left the steel lunchbox under the pier.", "wrong_object"),
        ("wrong-place", "not_identified", "Speaker 2: I left the steel toolbox inside the warehouse.", "wrong_location"),
        ("summary", "not_identified", "An editor's note speculates that Speaker 2 may have discussed a toolbox under a pier.", "editorial_inference"),
        ("speaker-three", "not_identified", "Speaker 3: Speaker 2 left the steel toolbox under the pier.", "hearsay_attribution"),
    ),
    "counsel-warrant-advice": (
        ("direct", "include", "To District Counsel Mira Chen: Please advise whether this search-warrant affidavit establishes probable cause.", "direct"),
        ("question", "include", "Counsel, do the facts in the search-warrant affidavit amount to probable cause? Please provide legal advice.", "question_request"),
        ("formal", "include", "The sender requests district counsel's legal opinion on probable cause in the proposed search-warrant affidavit.", "formal_request"),
        ("revision", "include", "Please advise district counsel whether the revised affidavit supports probable cause for the requested search warrant.", "revision"),
        ("scheduler", "not_identified", "Please schedule a meeting with district counsel about next week's search-warrant training.", "scheduling"),
        ("non-counsel", "not_identified", "To Pat in records: Does this search-warrant affidavit establish probable cause?", "wrong_recipient"),
        ("arrest", "not_identified", "Please ask district counsel whether probable cause supports the arrest warrant.", "wrong_warrant"),
        ("no-advice", "not_identified", "This message mentions the search-warrant affidavit but does not request legal advice.", "explicit_exclusion"),
        ("statement", "not_identified", "District counsel stated that search warrants generally require probable cause.", "no_request"),
        ("blank", "not_identified", "Subject: search-warrant affidavit and probable cause. Body: see attachment.", "keywords_only"),
        ("questionnaire", "not_identified", "The form asks whether anyone requested district counsel's advice; no answer is marked.", "question_only"),
        ("recipient-unknown", "not_identified", "Please advise whether this search-warrant affidavit establishes probable cause. The recipient is not identified.", "missing_recipient"),
    ),
    "final-dark-gray-correction": (
        ("direct", "include", "Correction: the vehicle was not red; its final recorded color is dark gray.", "direct"),
        ("revised", "include", "The revised vehicle report replaces red with dark gray as the final color.", "semantic_equivalent"),
        ("image-review", "include", "After image review, staff corrected the vehicle color from red to dark gray.", "correction"),
        ("register", "include", "Vehicle color | initial red | final dark gray | correction accepted.", "structured_row"),
        ("initial", "not_identified", "The initial witness description identified a red vehicle.", "initial_only"),
        ("uncertain", "not_identified", "The vehicle may have been dark gray rather than red.", "uncertainty"),
        ("other-final", "not_identified", "Correction: the final vehicle color is dark blue, not red.", "wrong_attribute"),
        ("gray-object", "not_identified", "A dark gray toolbox was recovered beside the red vehicle.", "wrong_object"),
        ("no-correction", "not_identified", "The vehicle is described as dark gray; no prior color or correction is recorded.", "missing_correction"),
        ("negated-gray", "not_identified", "The correction says the vehicle was not dark gray and remained recorded as red.", "negation"),
        ("question", "not_identified", "Was the final corrected vehicle color dark gray rather than red? No response follows.", "question_only"),
        ("two-vehicles", "not_identified", "The dark gray vehicle was unchanged; a separate red vehicle report was corrected for plate number.", "entity_mix"),
    ),
    "avery-system-corroboration": (
        ("badge", "include", "The badge system recorded Avery Lark entering the warehouse at 08:12.", "badge"),
        ("camera", "include", "Warehouse camera 2 independently shows Avery Lark inside at 08:14.", "camera"),
        ("exit", "include", "Avery Lark's warehouse badge registered an interior-door exit at 08:20.", "boundary"),
        ("facial", "include", "The independent camera index identifies Avery Lark in the warehouse lobby at 08:10.", "boundary"),
        ("self-report", "not_identified", "Avery Lark said she was inside the warehouse at 08:14.", "not_independent"),
        ("planned", "not_identified", "Avery Lark planned to enter the warehouse at 08:12.", "future_plan"),
        ("other-person", "not_identified", "The badge system recorded Avery Shore entering the warehouse at 08:12.", "wrong_entity"),
        ("wrong-time", "not_identified", "The badge system recorded Avery Lark entering the warehouse at 08:21.", "outside_window"),
        ("wrong-place", "not_identified", "The badge system recorded Avery Lark entering the library at 08:14.", "wrong_location"),
        ("offline", "not_identified", "The warehouse badge system was offline from 08:10 through 08:20 and recorded no entries.", "system_unavailable"),
        ("question", "not_identified", "Did a camera place Avery Lark in the warehouse at 08:15? The index is blank.", "question_only"),
        ("outside", "not_identified", "Camera 2 shows Avery Lark outside the warehouse at 08:14.", "wrong_position"),
    ),
}


def classification_cases() -> tuple[ClassificationCase, ...]:
    criteria = {item.criterion_id for item in review_criteria()}
    if set(_CLASSIFICATION_ROWS) != criteria:
        raise RuntimeError("classification gold does not cover every criterion")
    cases: list[ClassificationCase] = []
    for criterion_id, rows in _CLASSIFICATION_ROWS.items():
        for name, expected, text, challenge in rows:
            cases.append(
                ClassificationCase(
                    f"{criterion_id}:{name}",
                    criterion_id,
                    f"Generated {criterion_id} {name}.txt",
                    "This is a generated discovery-review fixture. " + text,
                    expected,
                    challenge,
                )
            )
    return tuple(cases)


def suite_fingerprint(document_count: int = DEFAULT_DOCUMENT_COUNT) -> str:
    payload = {
        "suite_id": GOLD_SUITE_ID,
        "schema_version": GOLD_SCHEMA_VERSION,
        "documents": [asdict(item) for item in retrieval_documents(document_count)],
        "retrieval_cases": [asdict(item) for item in retrieval_cases()],
        "criteria": [asdict(item) for item in review_criteria()],
        "classification_cases": [asdict(item) for item in classification_cases()],
    }
    canonical = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
