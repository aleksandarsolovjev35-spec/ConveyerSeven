# Production state-machine contract

This file is the executable project's short reference for the approved
seven-camera production automaton. The implementation-by-requirement status is
tracked in [`production_automaton_mapping.md`](production_automaton_mapping.md).

## Orthogonal state

The control truth is not one combinatorial enum. A complete `LineSnapshot`
contains independently owned `LineState`, `StepPhase`, `PendingIntent`,
`PauseContinuation`, `HealthState`, `DisplayState` and `PersistenceState`.
Their definitions and cross-field invariants live in `core/control_model.py`.
Pure transitions live in `core/line_reducer.py`; adapters never belong there.

```text
BOOTING -> RECOVERY_REQUIRED -> BOOTING
BOOTING -> IDLE -> RUNNING -> PAUSED -> RUNNING
                     |          |
                     +-------> STOPPING -> DISTRIBUTOR_HOME -> STOPPED
STOPPED -> RUNNING
IDLE/STOPPED -> SHUTTING_DOWN -> TERMINATED
any unfinished production state --fault--> FAULT
any non-terminated state --force exit--> SHUTTING_DOWN
```

`OFFLINE` is an HMI observation, never a `LineState`.

## Ownership and commands

`ControlCore` serializes typed events and is the only owner of its immutable
snapshot. `CommandArbiter` serializes operator commands and memoizes every
`command_id`; duplicate IDs return the saved result without repeating an
action. Safety priority is FORCE EXIT, STOP/EXIT, PAUSE, then guarded
START/RESUME/JOG.

STOP and PAUSE received during movement, inference, durable persistence or
REVIEW remain pending. STOP dominates PAUSE. EXIT has STOP physics plus an
`exit_after_drain` latch which a later STOP cannot erase. A five-second REVIEW
is never shortened by STOP or PAUSE.

## Physical step boundary

A production cell is exactly `19048 * 2 = 38096` firmware steps. Before the
single command, the transaction durably records its intent and latches INPUT
acceptance, pending transfer identity, route category/axis targets, ready epoch,
expected target, run ID and transaction ID.

`MotionTransaction`/`Conveyor` issue at most one movement command. Lost or
ambiguous acknowledgement causes telemetry polling only, never another command.
A software-cycle commit requires observed armed `TGT=38096`, changed
`lastReadyMs`, `LASTERR=0`, `MOV=WAIT=0`, and final
`POS=TGT=0, PAUSED=AUTO=1`. This is software-cycle evidence, not encoder proof.
`current_step` changes once and only at exact `STEP_COMMIT`.

Route preparation precedes movement and is held through commit. Physical
transfer updates counters and removes the Part before later archive work; a
traceability failure faults the line and must not re-sort the Part.

## Inspection and publication boundary

After confirmed motion, PAUSE is applied before SETTLE/snapshot. A resume from
`INSPECT_COMMITTED_STEP` performs a full SETTLE and new immutable capture. INPUT
(two roles) and CONTROL (five roles) may compute concurrently, but workers
return results only. PERSIST and PUBLISH receive one complete aggregate.

Empty INPUT increments only `empty_count`; it reserves no Part identity and
freezes no INPUT role. One-sided presence creates a Part and records
`input_presence_mismatch`. CONTROL requires exactly one expected Part at +4 and
runs all required models/rules before final category calculation.

`AtomicPublisher` accepts a complete `LineSnapshot`. Inspection result version
must equal the snapshot's logical `state_version`. Heavy images use independent
frame versions. Only roles containing a real Part are frozen, and only during
REVIEW.

## Drain, fault and recovery

STOPPING never accepts INPUT. With tracked Parts it executes ordinary steps with
`accept_input_for_step=false`. Once empty it sends no conveyor movement; it
homes and confirms both distributor axes before publishing STOPPED (or
SHUTTING_DOWN when EXIT was latched).

The first fault is immutable root cause. Later faults are secondary. FAULT has
no in-process path back to production. FORCE EXIT bypasses safe command gates,
issues no new movement, marks positions unknown and performs bounded cleanup.

Recovery and archive readers trust only committed durable boundaries. Staging
folders are never promoted by replay. Physical/HIL acceptance remains required
for motion, firmware reset, E-STOP, homing independence, camera/model timing and
calibrated SETTLE/REVIEW behavior.
