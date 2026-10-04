# Group Discussion Platform with Agentic AI Evaluation — Complete System Design

## 1. Introduction

The proposed Group Discussion (GD) platform is a real-time, multi-participant discussion system in which multiple candidates enter a common virtual discussion room, receive a discussion topic, communicate through audio/video, and are evaluated automatically by an Agentic AI evaluation system.

The purpose of the platform is not merely to determine whether a candidate gives a correct answer. A Group Discussion is fundamentally an interaction-oriented assessment. A candidate must demonstrate the ability to communicate ideas clearly, listen to others, respond constructively, contribute relevant arguments, think critically, collaborate, demonstrate leadership when appropriate, and maintain professional discussion behavior.

Therefore, the system must evaluate two dimensions simultaneously:

1. **What the candidate says**
2. **How the candidate behaves within the group**

The proposed architecture separates the real-time discussion infrastructure from the asynchronous AI evaluation infrastructure. This is essential because the live discussion must continue even when AI inference is delayed, unavailable, or temporarily overloaded.

The complete system therefore consists of:

```text
Candidate Applications
        ↓
Authentication
        ↓
GD Lobby
        ↓
Live GD Session
        ↓
Audio / Video + Real-Time Events
        ↓
Speech-to-Text + Speaker Diarization
        ↓
Transcript + Interaction Events
        ↓
Live Metrics + Rolling Session State
        ↓
GD Completion
        ↓
Agentic Evaluation Pipeline
        ↓
Specialized Evaluation Agents
        ↓
Score Aggregation
        ↓
Candidate Reports + GD Summary
```

---

# 2. Problem Statement

Traditional online Group Discussion platforms primarily provide communication infrastructure. They allow participants to join a room, speak to one another, and sometimes record the session. However, they generally do not provide a comprehensive mechanism for automatically understanding the entire conversation and evaluating each candidate within the context of the group.

A meaningful automated GD evaluation system must answer questions such as:

* What arguments did the candidate contribute?
* Were those arguments relevant?
* Did the candidate demonstrate domain knowledge?
* Did the candidate respond to opposing viewpoints?
* Did the candidate build upon another person's argument?
* Did the candidate listen to others?
* Did the candidate dominate the discussion?
* Did the candidate participate too little?
* Did the candidate interrupt others?
* Did the candidate demonstrate leadership?
* Did the candidate maintain logical reasoning?
* Did the candidate communicate clearly?
* Did the candidate help the discussion progress?
* Did the candidate exhibit collaborative behavior?

The central challenge is that these characteristics cannot reliably be inferred from a single isolated answer. They emerge from the **sequence of interactions among multiple participants**.

Therefore, the system must maintain both:

```text
Individual candidate context
+
Group-level conversation context
```

---

# 3. System Objectives / Aims

## 3.1 Primary Aim

The primary aim is to build a scalable real-time Group Discussion platform capable of conducting multi-participant online discussions and automatically generating explainable, candidate-specific evaluations using an Agentic AI architecture.

## 3.2 Specific Aims

### Aim 1 — Multi-user real-time discussion

Allow multiple candidates to participate in the same discussion session with low-latency audio/video communication.

### Aim 2 — Structured GD lifecycle

Provide a controlled lifecycle:

```text
Lobby Creation
→ Candidate Joining
→ Readiness
→ Topic Assignment
→ GD Start
→ Live Discussion
→ Timer
→ GD End
→ Evaluation
→ Report Generation
```

### Aim 3 — Accurate conversation capture

Capture the discussion as a timestamped transcript with speaker identity.

### Aim 4 — Speaker-aware transcription

Determine which candidate said which statement through speaker diarization or reliable speaker mapping.

### Aim 5 — Behavioral analysis

Measure objective interaction metrics such as:

* Speaking time
* Participation percentage
* Number of turns
* Number of interruptions
* Response latency
* Number of direct responses
* Number of questions
* Number of arguments introduced
* Number of arguments supported
* Number of counterarguments

### Aim 6 — Context-aware AI evaluation

Evaluate each candidate based on the complete discussion context rather than evaluating their speech in isolation.

### Aim 7 — Agentic evaluation

Use a supervisor/orchestrator and specialized evaluation agents to separately analyze different aspects of performance.

### Aim 8 — Incremental understanding

Process conversation in 30–60 second chunks to maintain an evolving understanding of the discussion without repeatedly sending the entire transcript to an LLM.

### Aim 9 — Complete final evaluation

After the session ends, combine the full transcript, final session state, behavioral metrics, and evaluation rubric to generate the final assessment.

### Aim 10 — Explainable scoring

Generate scores together with evidence, strengths, weaknesses, and reasons for the evaluation.

### Aim 11 — Scalability

Support many simultaneous GD rooms by horizontally scaling real-time servers and asynchronously scaling AI workers.

### Aim 12 — Fault isolation

Ensure an LLM failure does not terminate the GD session.

### Aim 13 — Personalization

Store candidate performance history so that future sessions can identify recurring weaknesses and recommend targeted practice.

### Aim 14 — Auditability

Retain sufficient evidence to allow a human evaluator or administrator to understand how the final score was generated.

---

# 4. Scope of the System

The platform contains two major subsystems.

## 4.1 Real-Time GD Subsystem

Responsible for:

* Authentication
* Lobby management
* Participant management
* Real-time audio/video
* Real-time events
* Session control
* Timer
* Recording
* Live metrics

## 4.2 AI Evaluation Subsystem

Responsible for:

* Speech-to-text
* Speaker identification
* Transcript organization
* Rolling state generation
* Behavioral interpretation
* Specialized evaluation
* Candidate scoring
* Report generation
* Performance history

The two subsystems are intentionally decoupled.

```text
REAL-TIME SYSTEM
       │
       │ events/data
       ↓
AI EVALUATION SYSTEM
```

The AI system is downstream from the real-time system.

---

# 5. Major Functional Requirements

## FR-01 — User Registration and Authentication

The system shall allow users to:

* Register
* Log in
* Log out
* Reset credentials
* Maintain authenticated sessions

The authentication subsystem shall issue an access token such as a JWT or use an OAuth-based identity system.

---

## FR-02 — User Profile

The platform shall maintain a candidate profile containing:

* User ID
* Name
* Email
* Skills
* Experience level
* Previous GD sessions
* Historical scores
* Strengths
* Weaknesses
* Recommended improvement areas

---

## FR-03 — GD Creation

An authorized host or administrator shall be able to create a GD session with:

* Topic
* Topic category
* Difficulty level
* Number of candidates
* Minimum participants
* Maximum participants
* Duration
* Preparation time
* Evaluation rubric
* Whether video is required
* Whether recording is enabled

Example:

```text
Topic:
"Should governments regulate generative AI?"

Duration:
15 minutes

Participants:
6–8

Category:
Technology / Policy

Difficulty:
Advanced
```

---

## FR-04 — Lobby Creation

The system shall create a temporary lobby associated with the GD session.

The lobby shall maintain:

* Lobby ID
* Session ID
* Candidate list
* Maximum capacity
* Current occupancy
* Readiness status
* Session status

---

## FR-05 — Join Lobby

Candidates shall be able to join the lobby using:

* Session code
* Invitation
* Direct link

The system must verify:

* User authentication
* Session validity
* Capacity
* Candidate eligibility

---

## FR-06 — Leave Lobby

A candidate shall be able to leave the lobby before the session begins.

The system must notify remaining users.

---

## FR-07 — Ready State

Each candidate shall have a ready state:

```text
WAITING
READY
NOT_READY
DISCONNECTED
```

The session shall not start until the configured start condition is met.

---

## FR-08 — Real-Time Audio/Video

The system shall allow all participants to communicate in real time.

The system should support:

* Microphone
* Camera
* Mute/unmute
* Camera on/off
* Connection status
* Device switching

---

## FR-09 — Real-Time Events

The platform shall propagate events such as:

```text
USER_JOINED
USER_LEFT
USER_READY
SPEECH_STARTED
SPEECH_STOPPED
TIMER_STARTED
TIMER_UPDATED
SESSION_STARTED
SESSION_ENDED
DISCONNECTED
RECONNECTED
```

---

## FR-10 — Session Timer

The system shall provide:

* Preparation timer
* GD timer
* Warning notifications
* Session end signal

Example:

```text
15:00 → Start
10:00 → Normal discussion
02:00 → Warning
00:00 → End
```

---

## FR-11 — Topic Delivery

The topic shall be delivered to all participants at the beginning of the session.

The system should prevent unauthorized modification of the topic once the GD starts.

---

## FR-12 — Audio Capture

The system shall capture participant speech for evaluation when recording/evaluation is enabled.

Audio should be processed in chunks rather than waiting until the end of the entire session.

---

## FR-13 — Speech-to-Text

The system shall convert spoken language to text.

The output should contain:

* Text
* Timestamp
* Candidate identity
* Confidence score
* Segment boundaries

---

## FR-14 — Speaker Diarization

The platform shall identify the speaker associated with each speech segment.

The final transcript should look like:

```text
00:00:03 Candidate A:
I believe AI will increase productivity.

00:00:11 Candidate B:
I disagree because automation can displace workers.

00:00:20 Candidate A:
I understand that concern, but...
```

---

## FR-15 — Transcript Storage

The complete raw transcript shall be stored throughout the session.

The transcript shall remain available after the session for evaluation and audit.

---

## FR-16 — Transcript Chunking

The system shall divide live transcripts into configurable processing windows, typically 30–60 seconds.

Each processing window shall contain the new transcript generated during that interval.

---

## FR-17 — Rolling Session State

The system shall maintain a compact state representing the current understanding of the GD.

The state shall contain information such as:

* Main arguments
* Candidate positions
* Key disagreements
* Important interactions
* Emerging behavioral observations
* Current discussion direction
* Unresolved questions

---

## FR-18 — Live Metrics

The system shall calculate metrics such as:

* Speaking duration
* Participation percentage
* Turn count
* Interruption count
* Response latency
* Direct response count
* Questions asked
* Arguments introduced
* Arguments supported
* Arguments challenged

---

## FR-19 — Event Streaming

The platform shall publish events to a message bus for downstream processing.

Examples:

```text
transcript.created
speech.started
speech.ended
participant.joined
participant.left
interaction.detected
metrics.updated
session.ended
evaluation.requested
evaluation.completed
```

---

## FR-20 — Post-GD Evaluation

When the GD ends, the system shall automatically create an evaluation job.

The job shall contain:

* Session metadata
* Complete transcript
* Final rolling state
* Behavioral metrics
* Evaluation rubric
* Candidate data relevant to assessment

---

## FR-21 — Agentic Orchestration

The system shall use a supervisor/orchestrator to determine the evaluation workflow.

The supervisor shall:

* Determine evaluation dimensions
* Select specialized evaluators
* Construct evaluation context
* Route candidate evidence
* Aggregate evaluator outputs
* Detect conflicts
* Trigger a final evaluation step if required

---

## FR-22 — Content Evaluation

The Content Agent shall evaluate:

* Relevance
* Argument quality
* Domain understanding
* Evidence
* Depth
* Critical thinking

---

## FR-23 — Communication Evaluation

The Communication Agent shall evaluate:

* Clarity
* Structure
* Conciseness
* Expression
* Confidence
* Ability to explain ideas

---

## FR-24 — Leadership Evaluation

The Leadership Agent shall evaluate:

* Initiative
* Direction
* Influence
* Ability to move the discussion forward
* Constructive leadership
* Appropriate ownership of ideas

Leadership must not simply mean "speaks the most."

---

## FR-25 — Collaboration Evaluation

The Collaboration Agent shall evaluate:

* Listening
* Building on others' ideas
* Responding constructively
* Supporting useful contributions
* Respect
* Team orientation

---

## FR-26 — Behavioral Evaluation

The Behavioral Agent shall evaluate:

* Participation
* Dominance
* Interruptions
* Turn-taking
* Engagement
* Interaction quality

This agent should combine transcript-based evidence with objective metrics.

---

## FR-27 — Candidate-Specific Evidence Construction

The system shall create an evidence packet for each candidate.

Example:

```text
Candidate A Evidence Packet

Shared GD Summary
+
A's contributions
+
A's responses
+
Responses to A
+
A's interaction graph
+
A's metrics
+
Relevant rolling-state observations
```

---

## FR-28 — Score Aggregation

The system shall combine evaluator outputs using a deterministic weighting mechanism.

Example:

```text
Content              30%
Communication        20%
Leadership           20%
Collaboration        20%
Behavior             10%
```

The exact weights should be configurable by the administrator.

---

## FR-29 — Feedback Generation

The system shall generate:

* Strengths
* Weaknesses
* Improvement suggestions
* Supporting evidence
* Overall feedback

---

## FR-30 — Report Generation

The system shall generate:

* Individual scorecard
* Detailed evaluation
* GD summary
* Group interaction summary
* Candidate comparison dashboard
* Downloadable report

---

## FR-31 — Historical Analysis

The platform shall store previous evaluations.

It should be able to identify:

```text
Repeated interruption
Low participation
Weak argument structure
Strong technical knowledge
Improved communication
```

---

## FR-32 — Notifications

The platform shall notify users about:

* Lobby creation
* Session invitation
* Session starting
* Session ending
* Evaluation completion
* Report availability

---

# 6. Non-Functional Requirements

## NFR-01 — Low Latency

Real-time communication should have low end-to-end latency.

The AI evaluation path must not block the real-time communication path.

---

## NFR-02 — Scalability

The platform shall support horizontal scaling of:

* WebSocket servers
* Media servers
* Speech-to-text workers
* AI evaluation workers
* Event consumers

---

## NFR-03 — Availability

A temporary failure of:

* LLM service
* Evaluation worker
* Analytics service

should not terminate an active GD.

---

## NFR-04 — Fault Tolerance

The system shall support:

* Retry
* Queue persistence
* Dead-letter queues
* Reconnection
* Partial service failure
* Evaluation recovery

---

## NFR-05 — Consistency

Persistent session state and final results must be strongly consistent.

Real-time ephemeral state may use eventual consistency where appropriate.

---

## NFR-06 — Security

The system shall protect:

* Authentication credentials
* Candidate information
* Audio/video
* Transcripts
* Evaluation results

Security mechanisms should include:

* TLS
* Token authentication
* Authorization
* Role-based access control
* Encryption at rest
* Secure object storage
* Access-controlled report retrieval

---

## NFR-07 — Privacy

Candidate recordings and transcripts are sensitive assessment data.

The system should define:

* Retention periods
* Deletion rules
* Consent mechanisms
* Access policies
* Audit logging

---

## NFR-08 — Explainability

Each final score should be traceable to supporting evidence.

Instead of:

```text
Leadership = 8.2
```

the system should provide:

```text
Leadership = 8.2

Evidence:
- Introduced a new direction in the discussion
- Reframed disagreement constructively
- Encouraged another participant to contribute
```

---

## NFR-09 — Observability

The platform shall record:

* Request latency
* WebSocket connection count
* Failed STT requests
* Queue depth
* LLM latency
* Token usage
* Evaluation failures
* Worker utilization
* Session failures

---

## NFR-10 — Cost Efficiency

The architecture should minimize unnecessary LLM calls.

This is one of the reasons for the rolling-state architecture.

---

# 7. Complete Component Architecture

## 7.1 Client Layer

The client can be implemented using:

* React
* Next.js
* Flutter
* React Native

Responsibilities:

* Login
* Lobby UI
* Camera/microphone control
* Participant display
* Timer
* Topic display
* Connection status
* Results dashboard

---

# 7.2 API Gateway

Responsibilities:

* Authentication routing
* REST API routing
* Rate limiting
* Request validation
* Load balancing
* API versioning

---

# 7.3 Authentication Service

Responsibilities:

* Login
* Registration
* Token generation
* Identity verification
* Authorization

---

# 7.4 Lobby Service

Responsibilities:

```text
Create lobby
Join lobby
Leave lobby
Manage participants
Track readiness
Assign topic
Set capacity
```

The lobby's highly dynamic state can be kept in Redis while permanent records are stored in PostgreSQL.

---

# 7.5 Session Service

Responsibilities:

* Create session
* Start session
* End session
* Manage timers
* Manage session status
* Emit session lifecycle events

A session could have:

```text
CREATED
WAITING
READY
STARTING
LIVE
ENDING
ENDED
EVALUATING
COMPLETED
FAILED
```

---

# 8. State Model

A session state machine can be defined as:

```text
CREATED
   ↓
WAITING
   ↓
READY
   ↓
STARTING
   ↓
LIVE
   ↓
ENDING
   ↓
ENDED
   ↓
EVALUATING
   ↓
COMPLETED
```

Failure branches:

```text
ANY STATE
   ↓
FAILED
```

Recovery may allow:

```text
FAILED
   ↓
RETRYING
   ↓
EVALUATING
```

---

# 9. Participant State

Each candidate can have:

```text
INVITED
   ↓
JOINED
   ↓
READY
   ↓
ACTIVE
   ↓
DISCONNECTED
   ↓
RECONNECTED
   ↓
ACTIVE
   ↓
LEFT
```

If a candidate disconnects temporarily, the session should continue.

---

# 10. Real-Time Communication Architecture

A suitable architecture is:

```text
Candidate Client
      │
      ▼
WebRTC
      │
      ▼
Media Server
      │
      ├── Candidate A
      ├── Candidate B
      ├── Candidate C
      └── Candidate N
```

WebSocket can separately manage:

```text
Client
  ↓
WebSocket Server
  ↓
Session Events
```

WebRTC is therefore responsible primarily for the live media plane, while WebSocket is primarily responsible for application/control events.

---

# 11. Audio Processing Pipeline

The speech path is:

```text
Microphone
   ↓
Audio Stream
   ↓
Audio Ingestion
   ↓
Chunking
   ↓
Noise Processing
   ↓
Speech-to-Text
   ↓
Speaker Diarization / Speaker Mapping
   ↓
Transcript Segment
   ↓
Transcript Service
```

A transcript segment can look like:

```json
{
  "session_id": "GD123",
  "candidate_id": "A",
  "start": 42.1,
  "end": 48.8,
  "text": "I think AI will create new categories of employment.",
  "confidence": 0.94
}
```

---

# 12. Why Speaker Diarization Is Critical

A standard transcript is insufficient.

The system must distinguish:

```text
Who spoke?
When?
For how long?
In what order?
To whom?
```

Without this, the AI cannot accurately evaluate:

* Response to another candidate
* Interruption
* Turn-taking
* Collaboration
* Leadership

---

# 13. Transcript Storage Strategy

The recommended architecture is:

```text
Live conversation
       ↓
Transcript segments
       ↓
Persist immediately
       ↓
Append to session transcript
```

Do not wait until the GD ends to store the transcript.

This protects against:

* Server failures
* Client crashes
* Network problems
* Worker failures

The raw transcript is the system's source of truth.

---

# 14. The Hybrid Chunking Architecture

This is the most important AI architectural decision.

The platform should use:

```text
RAW TRANSCRIPT
+
INCREMENTAL CHUNKS
+
ROLLING STATE
+
FINAL FULL-SESSION EVALUATION
```

The four layers serve different purposes.

## 14.1 Raw Transcript

Contains everything.

```text
00:00 A: ...
00:05 B: ...
00:10 C: ...
...
15:00 D: ...
```

This is never replaced by the summarized state.

---

## 14.2 Processing Chunk

Every 30–60 seconds, the system creates a processing window.

Example:

```text
Chunk 01
00:00–01:00

Chunk 02
01:00–02:00

Chunk 03
02:00–03:00
```

The chunk contains only newly available conversation.

---

## 14.3 Observer Agent

The Observer Agent receives:

```text
Previous Rolling State
+
New Chunk
+
Latest Objective Metrics
```

Its objective is:

> Understand what happened recently and update the overall session understanding.

It is not the final evaluator.

---

## 14.4 Rolling State

The Observer produces an updated state.

Example:

```json
{
  "main_arguments": [
    "AI increases productivity",
    "AI causes job displacement"
  ],
  "candidate_positions": {
    "A": "pro-automation",
    "B": "concerned about displacement",
    "C": "balanced"
  },
  "recent_interactions": [
    "A responded to B",
    "C supported A"
  ],
  "discussion_direction": "employment impact",
  "unresolved_questions": [
    "reskilling feasibility"
  ]
}
```

This state is a compressed memory.

---

# 15. The State Update Loop

The architecture is:

```text
             CHUNK 1
                +
          INITIAL STATE
                ↓
        OBSERVER AGENT
                ↓
             STATE 1

             CHUNK 2
                +
             STATE 1
                ↓
        OBSERVER AGENT
                ↓
             STATE 2

             CHUNK 3
                +
             STATE 2
                ↓
        OBSERVER AGENT
                ↓
             STATE 3
```

This continues until the GD ends.

This avoids repeatedly passing the entire transcript to the LLM.

---

# 16. What the Observer Agent Should NOT Do

It should not produce permanent final scores such as:

```text
A = 8.5
B = 7.1
```

at every minute.

Early behavior may change.

Instead it should produce observations:

```text
Candidate A is currently highly participative.

Candidate B has introduced several counterarguments.

Candidate C has low participation so far.
```

These are observations rather than final judgments.

---

# 17. Objective Metrics Pipeline

In parallel with the rolling state, a deterministic metrics engine operates.

```text
Speech Events
      ↓
Metrics Engine
      ↓
Candidate Metrics
```

Example:

```json
{
  "candidate_id": "A",
  "speaking_seconds": 245,
  "participation_ratio": 0.29,
  "turns": 17,
  "interruptions": 2,
  "responses": 8,
  "questions": 3
}
```

The system should prefer deterministic calculations for metrics that can be measured directly instead of asking an LLM to estimate them.

---

# 18. GD Completion

When the timer reaches zero:

```text
LIVE
  ↓
ENDING
  ↓
ENDED
```

The system finalizes:

* Transcript
* Speaker mapping
* Metrics
* Rolling state
* Interaction events

Then it creates:

```text
Evaluation Job
```

---

# 19. Evaluation Queue

The evaluation request enters a durable queue.

```text
Evaluation Request
        ↓
Evaluation Queue
        ↓
Judge Workers
```

The queue provides:

* Retry
* Backpressure
* Scheduling
* Parallel worker execution
* Failure isolation

---

# 20. Agentic Evaluation Workflow

The evaluation process follows:

```text
                GD ENDED
                    │
                    ▼
        ┌────────────────────────┐
        │ Evaluation Preparation │
        └───────────┬────────────┘
                    │
                    ▼
             Judge Supervisor
                    │
                    ▼
            Determine GD Type
            + Rubric + Context
                    │
                    ▼
        Select Specialized Agents
                    │
       ┌────────────┼────────────┐
       ↓            ↓            ↓
    Content    Communication  Behavior
       │            │            │
       ├────── Leadership ──────┤
       │            │            │
       └──── Collaboration ─────┘
                    │
                    ▼
             Evidence Results
                    │
                    ▼
             Score Aggregator
                    │
                    ▼
              Final Evaluator
                    │
                    ▼
                Report
```

---

# 21. Supervisor Agent

The Supervisor is responsible for orchestration rather than direct assessment.

Inputs:

```text
GD Topic
GD Type
Rubric
Full Transcript
Rolling State
Metrics
Candidate Profiles
```

Responsibilities:

1. Understand the evaluation task.
2. Determine which evaluation dimensions are needed.
3. Construct context.
4. Generate candidate evidence packets.
5. Invoke relevant evaluators.
6. Collect outputs.
7. Resolve missing/conflicting results.
8. Pass validated evidence to the aggregator.

---

# 22. Dynamic Agent Selection

The Supervisor can choose evaluators depending on the GD.

### Technical GD

```text
Content
Critical Thinking
Communication
Collaboration
Behavior
```

### Leadership GD

```text
Leadership
Communication
Collaboration
Behavior
```

### HR / Social GD

```text
Communication
Collaboration
Leadership
Behavior
Critical Thinking
```

This makes the system genuinely agentic.

---

# 23. Candidate Evidence Packets

All candidates share the same overall discussion context.

However, the final evaluator should also receive a candidate-specific evidence package.

Example:

```text
Shared Context
    +
Candidate Contributions
    +
Candidate Responses
    +
Responses Received
    +
Interaction Relationships
    +
Candidate Metrics
    +
Relevant Rolling State
```

This prevents the evaluation from becoming completely candidate-isolated while reducing unnecessary duplication of raw data.

---

# 24. Content Agent

The Content Agent asks:

* Were the arguments relevant?
* Were they correct?
* Did the candidate provide depth?
* Did they demonstrate knowledge?
* Did they use evidence?
* Did they reason critically?

Output:

```json
{
  "score": 8.4,
  "evidence": [
    "Introduced a relevant economic dimension",
    "Provided a clear causal argument"
  ],
  "weaknesses": [
    "Did not support one claim with evidence"
  ]
}
```

---

# 25. Communication Agent

Evaluates:

* Clarity
* Structure
* Articulation
* Conciseness
* Confidence
* Logical expression

It should evaluate actual communication quality, not microphone quality alone.

---

# 26. Leadership Agent

Leadership should not be defined as:

```text
Speaking most = Leadership
```

Instead, it should look for:

* Initiating useful directions
* Summarizing discussion
* Resolving disagreement
* Moving discussion forward
* Encouraging productive participation
* Influencing others constructively

---

# 27. Collaboration Agent

A strong GD participant should not only present their own opinion.

They should:

* Listen
* Respond
* Build on ideas
* Respect disagreement
* Integrate other arguments
* Support useful contributions

The Collaboration Agent focuses on these behaviors.

---

# 28. Critical Thinking Agent

Evaluates:

* Reasoning
* Assumption identification
* Trade-offs
* Counterarguments
* Cause-and-effect reasoning
* Synthesis of ideas

---

# 29. Behavior Agent

The Behavior Agent consumes both semantic and objective evidence.

Inputs:

```text
Transcript
+
Interaction Events
+
Speaking Metrics
+
Turn Data
+
Interruption Data
```

It evaluates:

* Dominance
* Participation balance
* Interruptions
* Turn-taking
* Engagement
* Discussion etiquette

---

# 30. Interaction Graph

A powerful representation is an interaction graph.

```text
A ─────→ B
│        │
│        ↓
└──────→ C
         │
         ↓
         D
```

For example:

```text
A → B
```

means A directly responded to or addressed B.

The graph can help identify:

* Who interacts with whom
* Who ignores others
* Who builds on ideas
* Who dominates
* Who has little interaction

---

# 31. Final Score Aggregation

The AI agent outputs must be combined through a deterministic aggregation layer.

Example:

```text
Content              30%
Communication        20%
Leadership           20%
Collaboration        20%
Behavior             10%
```

The aggregator calculates:

```text
Final Score
=
0.30 × Content
+
0.20 × Communication
+
0.20 × Leadership
+
0.20 × Collaboration
+
0.10 × Behavior
```

This makes the scoring policy explicit.

---

# 32. Why the Aggregator Should Be Separate

If an LLM directly decides:

```text
Final = 83
```

the scoring process can become difficult to reproduce.

A deterministic aggregator ensures:

* Consistency
* Reproducibility
* Configurable weighting
* Easier debugging
* Easier comparison across sessions

The LLM produces structured evidence and scores; the aggregation layer applies the formal scoring policy.

---

# 33. Final Report

The report should contain:

## Candidate Overview

```text
Candidate: A
Overall Score: 84/100
```

## Dimension Scores

```text
Content: 86
Communication: 82
Leadership: 79
Collaboration: 91
Behavior: 83
```

## Strengths

```text
Strong argument structure
Responded effectively to counterarguments
Built upon another participant's idea
```

## Weaknesses

```text
Occasional interruption
Could provide more evidence
Could summarize conclusions more clearly
```

## Evidence

The report should provide supporting examples from the transcript.

---

# 34. Group-Level Report

The system should also generate a group-level summary.

For example:

```text
Discussion quality:
High

Main themes:
1. Productivity
2. Employment displacement
3. Reskilling

Consensus:
Partial

Dominant participants:
A, B

Under-participating participants:
E, F

Major turning point:
C introduced the policy perspective
```

---

# 35. Data Storage Architecture

## PostgreSQL

Store:

```text
Users
Profiles
Lobbies
Sessions
Participants
Transcripts
Transcript segments
Scores
Reports
Evaluation records
Audit logs
```

## Redis

Store rapidly changing state:

```text
Active lobbies
Connected users
Session state
Timers
Temporary participant state
Caches
```

## Object Storage

Store:

```text
Audio
Video
Recordings
Large report files
Exports
```

## Vector Database (Optional)

Can store embeddings for:

* Historical reports
* Previous performance
* Relevant past feedback
* Semantic search across previous sessions

A vector database is not required for the basic GD workflow.

---

# 36. Event Bus Architecture

A Kafka/Redis Streams layer can carry:

```text
participant.joined
participant.left
speech.started
speech.ended
transcript.created
metrics.updated
interaction.detected
session.started
session.ended
evaluation.requested
evaluation.completed
report.generated
```

The event bus allows independent consumers:

```text
                 EVENT BUS
                    │
      ┌─────────────┼──────────────┐
      ↓             ↓              ↓
Metrics          Recording      AI Evaluation
Service           Service         Service
```

---

# 37. Why Event-Driven Architecture Is Useful

Suppose the transcript service produces:

```text
transcript.created
```

Then:

* Metrics Service needs it
* Rolling Observer needs it
* Storage needs it
* Analytics needs it

Instead of tightly coupling all services:

```text
Transcript Service
      ├──→ Metrics
      ├──→ AI
      └──→ Storage
```

you publish one event:

```text
Transcript Service
      ↓
    Event Bus
      ↓
Consumers
```

This improves scalability and service independence.

---

# 38. Failure Handling

## LLM Failure

If the LLM fails:

```text
GD
 ↓
continues normally
 ↓
evaluation waits in queue
```

The candidates are not affected.

## STT Failure

The system should:

* Retry
* Store raw audio
* Reprocess the affected segment

## Candidate Disconnect

The session continues.

The system records:

```text
candidate_disconnected
```

and later:

```text
candidate_reconnected
```

## Evaluation Worker Failure

The queue retries the job.

---

# 39. Idempotency

Evaluation jobs should be idempotent.

If:

```text
Evaluation Job 123
```

is accidentally processed twice, the system should not create two inconsistent final reports.

Use:

```text
session_id + evaluation_version
```

or a unique evaluation job identifier.

---

# 40. Versioning of Evaluation

Evaluation prompts and rubrics should be versioned.

For example:

```text
Rubric version = 2.1
Prompt version = 4.0
Agent version = 1.8
```

This allows the platform to answer:

> "Which evaluation configuration produced this score?"

This is valuable for research and debugging.

---

# 41. Observability

Important metrics include:

```text
Active GD sessions
Connected users
WebRTC connection failures
WebSocket connection count
STT latency
STT error rate
Transcript processing lag
Rolling-state processing latency
Evaluation queue depth
LLM latency
LLM token consumption
Evaluation success rate
Report generation latency
```

---

# 42. Scalability Architecture

At scale:

```text
                Load Balancer
                     │
      ┌──────────────┼──────────────┐
      ↓              ↓              ↓
   API Server     API Server     API Server
      │              │              │
      └──────────────┼──────────────┘
                     │
                   Redis
                     │
               Session State
```

For WebSocket servers:

```text
             Load Balancer
                  │
       ┌──────────┼───────────┐
       ↓          ↓           ↓
    WS-1        WS-2         WS-3
       └──────────┼───────────┘
                  ↓
              Redis Pub/Sub
```

For AI processing:

```text
                Evaluation Queue
              /       |        \
             ↓        ↓         ↓
         Worker 1  Worker 2   Worker 3
             │        │         │
             └────────┼─────────┘
                      ↓
                    LLM
```

---

# 43. End-to-End Example

Consider six candidates:

```text
A
B
C
D
E
F
```

Topic:

> "Should AI-generated content be regulated?"

### Step 1

Candidates authenticate.

```text
Client
 ↓
API Gateway
 ↓
Auth
```

### Step 2

Candidates enter lobby.

```text
GD #1024
A ✓
B ✓
C ✓
D ✓
E ✓
F ✓
```

### Step 3

All candidates become ready.

```text
Lobby
 ↓
Session Service
 ↓
START
```

### Step 4

Candidates talk using WebRTC.

### Step 5

Events are transmitted over WebSocket.

### Step 6

Audio is sent to STT.

### Step 7

Speaker diarization identifies:

```text
A
B
C
D
E
F
```

### Step 8

Transcript segments are stored immediately.

### Step 9

Every 30–60 seconds, an Observer Agent reads:

```text
New Chunk
+
Previous State
+
Metrics
```

and produces:

```text
Updated State
```

### Step 10

Metrics service calculates:

```text
Speaking time
Participation
Interruptions
Turns
Responses
```

### Step 11

At the end:

```text
Session = ENDED
```

### Step 12

System creates evaluation job.

### Step 13

Supervisor loads:

```text
Full Transcript
Final State
Metrics
Topic
Rubric
```

### Step 14

Supervisor selects agents:

```text
Content
Communication
Leadership
Collaboration
Behavior
```

### Step 15

For each candidate, the system constructs candidate evidence.

### Step 16

Agents evaluate candidates while retaining the shared GD context.

### Step 17

Results are collected.

### Step 18

Score Aggregator calculates final scores.

### Step 19

Final Output Agent generates feedback.

### Step 20

Report is stored and sent to candidate.

---

# 44. The Complete Information Flow

The platform can be understood through four parallel pipelines.

## Pipeline A — Real-Time Communication

```text
Candidate
 ↓
WebRTC
 ↓
Media Server
 ↓
Other Candidates
```

## Pipeline B — Speech Processing

```text
Audio
 ↓
Audio Ingestion
 ↓
STT
 ↓
Diarization
 ↓
Transcript
```

## Pipeline C — Live Understanding

```text
Transcript Chunk
+
Previous State
+
Metrics
 ↓
Observer Agent
 ↓
Rolling State
```

## Pipeline D — Final AI Evaluation

```text
Full Transcript
+
Final State
+
Metrics
+
GD Metadata
 ↓
Supervisor
 ↓
Specialized Agents
 ↓
Score Aggregator
 ↓
Final Report
```

---

# 45. Complete State Architecture

The system should maintain state at multiple levels.

## Lobby State

```text
CREATED
WAITING
READY
STARTING
```

## Session State

```text
LIVE
ENDING
ENDED
EVALUATING
COMPLETED
```

## Candidate State

```text
INVITED
JOINED
READY
ACTIVE
DISCONNECTED
RECONNECTED
LEFT
```

## Evaluation State

```text
QUEUED
PREPARING
RUNNING
WAITING_FOR_AGENT
AGGREGATING
REPORT_GENERATING
COMPLETED
FAILED
```

This separation prevents one state machine from becoming unnecessarily complicated.

---

# 46. Agent State

The Observer Agent can maintain:

```text
INITIALIZED
OBSERVING
UPDATING_STATE
STATE_UPDATED
PAUSED
FAILED
```

The evaluation agents can maintain:

```text
WAITING
RUNNING
COMPLETED
FAILED
```

The Supervisor can maintain:

```text
INITIALIZED
PLANNING
DISPATCHING
COLLECTING
VALIDATING
AGGREGATING
COMPLETED
```

---

# 47. Why There Are Two Different Kinds of "Memory"

This is an important conceptual distinction.

## Raw Memory

The complete transcript:

```text
Everything that happened
```

## Semantic Memory

The rolling state:

```text
What the system currently understands about what happened
```

Therefore:

```text
Raw transcript = exact evidence
Rolling state = compressed understanding
```

Neither replaces the other.

---

# 48. Why the Full Transcript Is Still Needed

Even though the Observer Agent creates a rolling state, the final evaluator should have access to the original evidence.

Why?

Because a summary can omit something important.

For example, the rolling state may say:

```text
A challenged B's argument.
```

But the final evaluator may need the exact exchange to determine whether A:

* challenged respectfully
* misunderstood B
* used evidence
* changed the discussion direction

Therefore:

```text
Final Evaluation
=
Rolling State
+
Original Evidence
```

---

# 49. Context Optimization

The system should avoid sending enormous duplicate prompts to every agent.

Instead:

## Shared session summary

Contains:

```text
Topic
Main arguments
Group dynamics
Discussion progression
Major disagreements
```

## Candidate evidence

Contains:

```text
Candidate contributions
Candidate responses
Interactions involving candidate
Metrics
Relevant transcript excerpts
```

This provides sufficient context while reducing unnecessary token usage.

---

# 50. Evaluation Output Contract

Every agent should return structured output.

Example:

```json
{
  "candidate_id": "A",
  "dimension": "collaboration",
  "score": 8.7,
  "confidence": 0.89,
  "strengths": [
    "Built on B's argument",
    "Responded directly to disagreement"
  ],
  "weaknesses": [
    "Could have invited another participant"
  ],
  "evidence": [
    {
      "timestamp": "05:14",
      "summary": "A acknowledged B's concern and extended it."
    }
  ]
}
```

Structured output makes downstream processing much easier.

---

# 51. Confidence and Uncertainty

The system should not treat every AI evaluation as equally certain.

An evaluator may produce:

```text
score = 8.2
confidence = 0.91
```

If the evidence is weak:

```text
score = 7.8
confidence = 0.55
```

The Supervisor can use confidence to determine whether additional evaluation or human review is required.

---

# 52. Human-in-the-Loop Capability

For high-stakes assessment, the platform should support human review.

Example:

```text
AI score
   ↓
Confidence check
   │
   ├── High confidence → Finalize
   │
   └── Low confidence → Human Review
```

A human evaluator can review:

* Transcript
* Evidence
* Metrics
* Agent outputs
* Final score

---

# 53. Personalization Layer

After evaluation:

```text
Current GD
    ↓
Candidate Performance Profile
    ↓
Historical Analysis
    ↓
Weakness Detection
    ↓
Future Practice Recommendation
```

For example:

```text
Historical weakness:
Low participation in GDs

Current GD:
Improved participation

Recommendation:
Practice structured disagreement and concise argument introduction.
```

This turns the GD subsystem into a continuous learning component of the broader interview platform.

---

# 54. Recommended Technology Stack

A possible implementation stack is:

## Frontend

```text
React / Next.js
```

## Backend APIs

```text
FastAPI / Node.js
```

## Real-time communication

```text
WebRTC
WebSocket
```

## Media infrastructure

```text
LiveKit / mediasoup / equivalent media server
```

## Database

```text
PostgreSQL
```

## Fast state/cache

```text
Redis
```

## Event streaming

```text
Kafka
```

or Redis Streams for a smaller system.

## Speech-to-text

```text
Whisper / managed ASR provider
```

## LLM layer

```text
LLM API / self-hosted model
```

## Object storage

```text
S3 / MinIO
```

## Observability

```text
Prometheus
Grafana
Structured logs
```

The exact technologies can change; the architecture should remain independent of individual vendors.

## Deployment target for this implementation — local only

This build is **not hosted anywhere**. Every component above runs on the developer's machine and is reached over `localhost`; the infrastructure services (PostgreSQL, Redis, LiveKit, MinIO, Prometheus, Grafana) come up through a single local `docker compose`, and the frontend and backend run as local dev servers against them.

Concretely, for this implementation:

```text
Media server      LiveKit, self-hosted in Docker   ws://localhost:7880
Object storage    MinIO (S3-compatible), local     http://localhost:9000
Speech-to-text    Whisper, run locally
Event streaming   Redis Streams (not Kafka)
```

There is no cloud account, no managed service, no public URL, and no deploy pipeline. This is a deployment constraint only — the architecture, the decoupling, the contracts, and the evaluation design are unchanged, and the vendor-independence requirement still holds. See the local port map in `claude.md`.

---

# 55. MVP Architecture

A first implementation should not immediately build every component.

The MVP can be:

```text
React
 ↓
FastAPI
 ↓
WebSocket/WebRTC
 ↓
Redis
 ↓
Speech-to-Text
 ↓
PostgreSQL
 ↓
One Observer Agent
 ↓
One Final Judge
 ↓
Report
```

After that, add:

```text
Kafka
Multiple Agent Evaluators
Behavior Analytics
Dynamic Supervisor
Object Storage
Horizontal Scaling
```

This reduces implementation complexity while preserving the architectural direction.

---

# 56. Production Architecture

The production architecture becomes:

```text
                         CLIENTS
                    /      |       \
                   /       |        \
                  ↓        ↓         ↓
              API Gateway / Load Balancer
                         │
             ┌───────────┼─────────────┐
             ↓           ↓             ↓
           Auth        Lobby         Session
             │           │             │
             └───────────┼─────────────┘
                         ↓
                 WebRTC / Media
                         │
                    LIVE GD ROOM
                         │
                  WebSocket Events
                         │
                         ↓
                     Event Bus
                         │
        ┌────────────────┼──────────────────┐
        ↓                ↓                  ↓
   Transcript        Metrics            Recording
     Service         Service              Service
        │                │
        ↓                ↓
   Full Transcript   Objective Metrics
        │                │
        └───────────┬────┘
                    ↓
              Rolling State
                    │
               Observer Agent
                    │
                    ↓
             Final Session State
                    │
                  GD END
                    │
                    ↓
            Evaluation Queue
                    │
                    ↓
           Supervisor Agent
                    │
        ┌───────────┼────────────┐
        ↓           ↓            ↓
     Content   Communication   Behavior
        ↓           ↓            ↓
   Leadership  Collaboration  Critical
                              Thinking
        └───────────┬────────────┘
                    ↓
              Score Aggregator
                    ↓
              Final Report
                    ↓
      PostgreSQL + Object Storage
```

---

# 57. Most Important Architectural Principles

The complete system should follow these principles.

### Principle 1 — Real-time and AI are decoupled

```text
Live GD ≠ LLM inference
```

The GD must continue even when the AI system is slow.

### Principle 2 — Store the complete transcript

The raw transcript is the source of truth.

### Principle 3 — Use incremental chunks for understanding

30–60 second chunks feed the Observer Agent.

### Principle 4 — Maintain rolling state

State prevents the LLM from repeatedly receiving the entire history during the session.

### Principle 5 — Final evaluation uses complete evidence

Full transcript + final state + metrics.

### Principle 6 — GD evaluation is contextual

Candidates should not be judged as isolated speakers.

### Principle 7 — Use candidate-specific evidence packets

Evaluate an individual candidate using their contributions and interactions while retaining group context.

### Principle 8 — Use deterministic metrics wherever possible

Do not ask an LLM to calculate speaking time if the system can calculate it directly.

### Principle 9 — Use specialized agents

Separate content, communication, leadership, collaboration, critical thinking, and behavior.

### Principle 10 — Use a Supervisor

The Supervisor manages the evaluation workflow rather than becoming one giant prompt.

### Principle 11 — Use deterministic aggregation

LLMs provide structured evidence; the aggregation layer produces the final weighted score.

### Principle 12 — Preserve evidence

Every score should be explainable through transcript evidence and measurable behavior.

---

# 58. Final Conceptual Model

The whole system can ultimately be summarized as:

```text
                 GROUP DISCUSSION
                       │
                       ▼
              REAL-TIME ROOM
                       │
             ┌─────────┴──────────┐
             │                    │
             ▼                    ▼
          AUDIO               EVENTS
             │                    │
             ▼                    ▼
      STT + DIARIZATION      METRICS
             │                    │
             ▼                    │
       FULL TRANSCRIPT            │
             │                    │
             ▼                    │
      30–60 SEC CHUNKS            │
             │                    │
             ▼                    │
       OBSERVER AGENT             │
             │                    │
             ▼                    │
       ROLLING STATE              │
             │                    │
             └─────────┬──────────┘
                       │
                    GD ENDS
                       │
                       ▼
            FULL EVIDENCE PACKAGE
                       │
          ┌────────────┼────────────┐
          │            │            │
          ▼            ▼            ▼
      Transcript   Rolling State  Metrics
          │            │            │
          └────────────┼────────────┘
                       ▼
                JUDGE SUPERVISOR
                       │
                 Dynamic Routing
                       │
        ┌──────────────┼──────────────┐
        ↓              ↓              ↓
     Content      Communication   Leadership
        │              │              │
        ├──────────────┼──────────────┤
        ↓              ↓              ↓
 Collaboration   Critical Thinking  Behavior
        │              │              │
        └──────────────┼──────────────┘
                       ▼
                SCORE AGGREGATOR
                       │
                       ▼
                FINAL REPORT
                       │
                       ▼
             HISTORICAL PROFILE
                       │
                       ▼
              PERSONALIZED PRACTICE
```

The fundamental design philosophy is therefore:

> **Capture everything, summarize continuously, evaluate contextually, score deterministically, and explain every decision with evidence.**

This architecture gives the Group Discussion module four distinct capabilities at once: **real-time collaboration, reliable conversation capture, contextual agentic reasoning, and measurable candidate evaluation.**
