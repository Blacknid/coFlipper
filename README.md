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

These three directions are not exhaustive; they are a starting point for validating the concept within the time available for the contest. As the implementation advances, other Flipper Zero capabilities (such as Sub-GHz, low-frequency RFID, or the GPIO module) can be integrated in the same way.

## Proposed architecture

The project is organized around two main components, mirrored in the repository structure:

- flipper/ — logica destinată să ruleze pe (sau în relație directă cu) dispozitivul Flipper Zero: comunicarea cu modulele sale radio, IR și NFC, precum și expunerea acestor capabilități către restul sistemului.
- desktop/ — componenta agentică propriu-zisă, responsabilă de interpretarea cererilor utilizatorului, decizia asupra pașilor necesari și orchestrarea comenzilor trimise către Flipper Zero. Include aplicația cu interfață grafică prin care utilizatorul interacționează efectiv cu sistemul.

Separarea celor două componente urmărește un principiu simplu: dispozitivul rămâne executantul operațiunilor de nivel jos, în timp ce agentul concentrează întreaga logică de interpretare și decizie, fiind singurul punct cu care utilizatorul interacționează direct, în limbaj natural.

Comunicarea dintre cele două componente se face prin portul serial USB al Flipper Zero, folosind un protocol text propriu, denumit CFP (coFlipper Protocol) și documentat integral în PROTOCOL.md.

The link between the language model and the device is realized through a declarative command catalog, commands.json. This is the system's single source of truth: every command described there is automatically converted, when the agent starts, into a tool the model can call (*function calling*). Adding a new capability therefore means describing it in the catalog and implementing it in firmware, with no changes to the agent's logic.

Fluxul complet al unei cereri este următorul: utilizatorul formulează o intenție în limbaj natural; modelul decide care comenzi din catalog sunt necesare și le solicită; agentul le traduce în cadre CFP și le trimite pe portul serial; Flipper Zero le execută și răspunde; rezultatele reale sunt returnate modelului, care formulează pe baza lor răspunsul final.

Interacțiunea are loc într-o aplicație cu interfață grafică, organizată în două panouri: conversația și, permanent vizibil alături de ea, lanțul de raționament al agentului — raționamentele modelului și comenzile executate efectiv pe dispozitiv, în ordinea în care s-au produs. Această a doua zonă are o funcție care depășește depanarea: permite utilizatorului să verifice că afirmațiile agentului se sprijină pe măsurători reale, nu pe formulări plauzibile.

One design constraint we considered essential is that the model is not permitted to make claims about the state of the hardware in the absence of an actual result received from the device. If a command fails or is not yet implemented, the agent states this explicitly instead of producing a plausible but fabricated answer. Without this restriction, a conversational assistant applied to a technical domain could generate seemingly credible data — frequencies, card identifiers, protocols — that corresponds to no real measurement.

## Design philosophy

A central aspect of this project is delegating the "how" decisions to the agent, leaving the user only with phrasing the intent — the "what" they want. This separation is inspired by the paradigm of agents able to operate external instruments (*tool use*), in which natural language becomes the primary interface and the translation into concrete technical commands is the intermediate layer's responsibility.

## Elements of originality

Unlike existing companion applications for the Flipper Zero, which expose the device's functionality through a traditional graphical interface, coFlipper proposes a conversational interface as the central point of interaction. The user does not navigate manually through a menu of options, but describes the desired outcome, and it is the agent that chooses and chains the operations needed on the device.

It is important to state precisely where this project's originality lies and where it does not. The individual commands the Flipper Zero exposes through the CFP protocol — reading a Sub-GHz signal, decoding an infrared signal, reading or emulating an NFC tag — are not original: they reproduce functionality the device already has natively, in its factory applications. The full catalog of these commands is documented in commands.json, under the `"layer": "device"` label.

The project's originality lies at a layer above these, marked in the same file under the `"layer": "agent"` label: operations that combine one or more device-level commands with reasoning performed by the language model, in order to produce an interpreted answer rather than just raw data. For example, instead of displaying a Sub-GHz protocol code, the agent can explain, in natural language, what type of device is likely the source of the signal; instead of listing NFC UIDs, it can build a summary of a monitoring session. This is the layer that, to the best of our knowledge, has no direct equivalent in the existing ecosystem of applications for the Flipper Zero.

<<<<<<< HEAD
O a doua contribuție, complementară celei de mai sus, este lanțul de raționament expus permanent utilizatorului. Aplicațiile conversaționale obișnuite arată doar întrebarea și răspunsul, iar drumul dintre ele rămâne ascuns; în cazul unui agent care acționează asupra hardware-ului, acest drum este tocmai partea care poate fi verificată. coFlipper afișează, pas cu pas și în timp real, raționamentele modelului alături de comenzile pe care acestea le-au motivat, cu argumentele și răspunsurile primite de la dispozitiv. Utilizatorul poate astfel urmări nu numai ce a răspuns agentul, ci și de ce a ales să facă tocmai acele măsurători — iar când o comandă eșuează, se vede exact ce a eșuat și în ce moment al raționamentului. Detaliile de implementare sunt documentate în desktop/README.md.

## Stadiul curent și limitări
=======
## Current status and limitations
>>>>>>> 68f3ae9d0fb859fc675335475bc395bf37f8ebcb

The project was developed within the OPEN section of the InfoEducație 2026 national stage, in the time allotted to it. As a result, the implementation reflects an early stage — a working prototype — focused on validating the concept rather than exhaustively covering all Flipper Zero capabilities. Extending and consolidating the project remain natural directions for a possible continuation after the competition.

## Statement of originality

In accordance with the InfoEducație regulations, the project components that do not belong entirely to the authors (external libraries, borrowed code fragments, graphical resources, etc.) are explicitly listed in the separate originality file attached to the submission.
