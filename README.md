# coFlipper

Olympiad of Digital Innovation and Creation — InfoEducație 2026
National stage, OPEN section

| | |
|---|---|
| Team leader | Lupu Iulian-Nicolae |
| Teammate | Ciuca Andrei-Corneliu |
| Grade | 10th |
| Institution | "Atanasie Marienescu" High School |

---

## General description

coFlipper is a project aimed at developing an agentic harness for integration with the Flipper Zero device. We start from the observation that existing tools for the Flipper Zero remain, in essence, manual control utilities: the user selects a frequency, starts a capture, and interprets the result on their own. coFlipper adds an additional layer of mediation between the user and the device — an agent capable of understanding an intent expressed in natural language and translating it into concrete operations on the hardware.

## Motivation

Current interaction with the Flipper Zero requires, in most cases, a certain level of prior technical knowledge: the user has to know which frequency to probe, how to read a captured signal, or what a particular NFC protocol means. This barrier to entry limits the device's accessibility for those who do not come from a solid technical background.

Our working hypothesis is that an agentic layer, capable of processing freely phrased requests and turning them into actions on the Flipper, can significantly lower that barrier. Instead of the user learning the device's syntax and internal logic, it is enough to describe what they want to find out or do, and the agent identifies the necessary steps and carries them out.

## Objectives

Within the OPEN section, we set out to explore and implement the following functional directions:

- Radio frequency analysis — the agent's ability to probe a specific frequency and return relevant contextual information about it (for example, what type of device or protocol it appears to be associated with, based on the captured signal).
- Infrared interaction — using the Flipper Zero's IR module to identify and, potentially, replicate signals emitted by remote controls or other compatible devices.
- NFC interaction — an illustrative scenario for this direction would be one where the user points the agent at a particular mobile phone, and the agent builds, based on that request, a routine that allows observing the entities or access points that the phone's NFC module comes into contact with.

These three directions are not exhaustive; they are a starting point for validating the concept within the time available for the contest. Of the three, radio frequency analysis and infrared are the ones carried through to working hardware and confirmed there: the agent measures the signal level on a requested frequency and interprets it, and drives the IR bruteforce over the same firmware bridge. NFC reading (`nfc.read`, `nfc.watch`) is implemented and compiles against the real SDK, but is not yet flashed and confirmed on a physical tag - see [/flipper/README.md](flipper/README.md) for exactly what that means and the one build detail worth knowing before relying on it. NFC emulation (`nfc.emulate`, `nfc.stop`) remains unimplemented, marked `"status": "stub"` in the catalog so the model is warned in the tool description itself rather than finding out by a failed call. As the implementation advances, the remaining Flipper Zero capabilities (NFC emulation, low-frequency RFID, the GPIO module, Sub-GHz capture and replay) can be integrated in the same way, since adding a command means describing it in the catalog rather than changing the agent.

## Proposed architecture

The project is organized around two main components, mirrored in the repository structure:

- flipper/ — the logic meant to run on (or in direct relation to) the Flipper Zero device: communication with its radio, IR and NFC modules, and the exposure of those capabilities to the rest of the system.
- desktop/ — the agentic component proper, responsible for interpreting the user's requests, deciding on the steps required and orchestrating the commands sent to the Flipper Zero. It includes the graphical application through which the user actually interacts with the system.

The separation of the two components follows a simple principle: the device remains the executor of low-level operations, while the agent concentrates the whole of the interpretation and decision logic, being the only point the user interacts with directly, in natural language.

Communication between the two components goes over the Flipper Zero's USB serial port, using a text protocol of our own, named CFP (coFlipper Protocol) and documented in full in PROTOCOL.md.

The link between the language model and the device is realized through a declarative command catalog, commands.json. This is the system's single source of truth: every command described there is automatically converted, when the agent starts, into a tool the model can call (*function calling*). Adding a new capability therefore means describing it in the catalog and implementing it in firmware, with no changes to the agent's logic.

The complete flow of a request is as follows: the user phrases an intent in natural language; the model decides which commands from the catalog are needed and requests them; the agent translates them into CFP frames and sends them over the serial port; the Flipper Zero executes them and responds; the real results are returned to the model, which phrases the final answer on their basis. For work that needs many successive measurements, or that only has to research what the session already did, the agent can delegate to a specialised subagent instead of doing it inline. Several such conversations can run in parallel, and their results are merged, by subject, at the end.

The agentic loop has the memory and the limits such a loop needs. Within a conversation the model sees the whole history, and when that history grows long it is compacted into a summary so the context stays bounded; across conversations, the agent keeps a small persistent memory of durable facts it chose to remember, loaded back into every new session. It halts on its own: a turn ends when no further command is requested, and every subagent runs under a fixed budget.

One design constraint we considered essential is that the model is not permitted to make claims about the state of the hardware in the absence of an actual result received from the device. If a command fails or is not yet implemented, the agent states this explicitly instead of producing a plausible but fabricated answer. Without this restriction, a conversational assistant applied to a technical domain could generate seemingly credible data — frequencies, card identifiers, protocols — that corresponds to no real measurement.

The interaction takes place in a graphical application organised into two panels: the conversation and, permanently visible beside it, the agent's reasoning chain.

## Design philosophy

A central aspect of this project is delegating the "how" decisions to the agent, leaving the user only with phrasing the intent — the "what" they want. This separation is inspired by the paradigm of agents able to operate external instruments (*tool use*), in which natural language becomes the primary interface and the translation into concrete technical commands is the intermediate layer's responsibility.

## Elements of originality

Unlike existing companion applications for the Flipper Zero, which expose the device's functionality through a traditional graphical interface, coFlipper proposes a conversational interface as the central point of interaction. The user does not navigate manually through a menu of options, but describes the desired outcome, and it is the agent that chooses and chains the operations needed on the device.

It is important to state precisely where this project's originality lies and where it does not. The individual commands the Flipper Zero exposes through the CFP protocol — reading a Sub-GHz signal, decoding an infrared signal, reading or emulating an NFC tag — are not original: they reproduce functionality the device already has natively, in its factory applications. The full catalog of these commands is documented in commands.json, under the `"layer": "device"` label.

The project's originality lies at a layer above these, marked in the same file under the `"layer": "agent"` label: operations that combine one or more device-level commands with reasoning performed by the language model, in order to produce an interpreted answer rather than just raw data. For example, instead of displaying a Sub-GHz protocol code, the agent can explain, in natural language, what type of device is likely the source of the signal; instead of listing NFC UIDs, it can build a summary of a monitoring session. This is the layer that, to the best of our knowledge, has no direct equivalent in the existing ecosystem of applications for the Flipper Zero.

Within that layer, the agent is not a single model answering questions. When a job needs many successive measurements, or when the question is about what the session has already done rather than about the present moment, the agent delegates it to a specialised subagent: a separate conversation with its own instruction, its own restricted set of tools and its own budget, which reports back with both a conclusion and the raw readings behind it. The analyst subagent is given no device tools at all, precisely so that a component that only reads cannot become the source of a measurement nobody took. Delegation is described in the same catalog as everything else, so a new specialist is a matter of description rather than of code.

The same layer is where the agent builds actual Flipper Zero applications on request. Asked for an app, it does not answer with a single model call but runs a three-way design debate — a proposer that writes the C source, a challenger that argues against it, and an arbiter that keeps only what survives the argument — then compiles the result with the Flipper toolchain and, if a device is attached, installs it. The generated source is kept on disk and stays editable: a later request to change an app reloads its source and runs the same debate over it. This is documented in full in APP_BUILDER.md.

A second contribution, complementary to the one above, is the reasoning chain shown permanently to the user. Ordinary conversational applications display only the question and the answer, leaving the path between them hidden; in the case of an agent that acts upon hardware, that path is precisely the part that can be verified. coFlipper displays, step by step and in real time, the model's reasoning alongside the commands that reasoning motivated, with their arguments and the responses received from the device. The user can therefore follow not only what the agent answered, but why it chose to take those particular measurements — and when a command fails, it is visible exactly what failed and at which point in the reasoning.

Delegation enters the same chain rather than hiding behind it. The moment a subagent is summoned, the chain announces who it is, what it is permitted to do and what it was asked; its own reasoning and commands appear nested one level deeper, in a different accent colour; and it ends by reporting back to the agent that summoned it. The three debating agents of the app builder appear through this same mechanism, so the argument that produced an app is as visible as the app itself. Without this, part of the answer would have been produced by a second model without the user ever learning of its existence. Implementation details are documented in desktop/README.md.

## Current status and limitations

The project was developed within the OPEN section of the InfoEducație 2026 national stage, in the time allotted to it. As a result, the implementation reflects an early stage — a working prototype — focused on validating the concept rather than exhaustively covering all Flipper Zero capabilities. Extending and consolidating the project remain natural directions for a possible continuation after the competition.

## Statement of originality

In accordance with the InfoEducație regulations, the project components that do not belong entirely to the authors (external libraries, borrowed code fragments, graphical resources, etc.) are explicitly listed in the separate originality file attached to the submission.
