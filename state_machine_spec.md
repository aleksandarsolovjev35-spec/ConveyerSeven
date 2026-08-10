# Production state-machine contract

This file is the executable project's short reference for the approved
seven-camera production specification.  The detailed production requirements
remain in the release specification supplied with the project.

## State ownership

`ControlRuntime` is the only owner of state transitions, the operational log,
and hardware ports.  HMI commands enter `CommandArbiter`; every command has a
unique `command_id`, is serialized, and is idempotent.  A duplicate command ID
returns the original result without repeating the action.

```text
IDLE --START--> RUNNING --STOP--> STOPPING --EMPTY--> STOPPED
                 |                     |
                 +--PAUSE--> PAUSED --RESUME--+

IDLE/RUNNING/PAUSED/STOPPING/STOPPED --FAULT--> FAULT
```

`STOP` and `PAUSE` received during movement, inference, durable persistence, or
REVIEW are pending.  They do not cancel that transaction; STOP has priority and
is applied before the next movement.  FAULT has no in-process reset or resume.

## Step boundary

A production cell is exactly `19048 * 2 = 38096` firmware steps.  The adapter
proves, in one command cycle, reset `POS=TGT=0`, observed `TGT=38096`, an
in-range armed position, a changed `lastReadyMs`, and final
`POS=TGT=0, MOV=WAIT=0, PAUSED=AUTO=1, LASTERR=0`.  Missing or ambiguous
telemetry never causes a second movement command.

`current_step` changes once, only after that complete proof.  A part's position
is always `confirmed_current_step - birth_step`; JOG never changes logical
steps.  After JOG the next production command requires a newly proven firmware
reset state.

## Inspection boundary

All seven physical live readers stay active for the lifetime of the process.
After stop and calibrated SETTLE, the first complete frame after the freshness
boundary is copied from the live buffer.  INPUT presence and CONTROL receive
the same immutable stage snapshot and run in parallel.  A result publication is
atomic; before it HMI is live, after it only roles with a real part are frozen
for one server-monotonic five-second REVIEW.

A malformed camera/model/rule result is a technical FAULT.  An empty INPUT
cell increments only `empty_count`.  A one-sided INPUT positive creates a Part
and `input_presence_mismatch`; different positive counts do not.

## Persistence boundary

Only a confirmed Part gets evidence buffering.  A finalized part is written to
`staging/`, every file is fsynced, an exact SHA-256 manifest and commit marker
are fsynced, and the complete catalog is atomically renamed to `parts/`.  A
staging directory is never auto-promoted.  Counters, recent parts, and the
committed index use only verified catalogs.  Archive failure after physical
transfer is a traceability FAULT and never a retry.
