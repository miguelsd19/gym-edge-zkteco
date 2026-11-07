# Fingerprint enrollment on Norias SpeedFace 6.60

The November 7 probe opened the fingerprint prompt, but pyzk 0.9 treated
one-byte progress notifications as fixed-layout enrollment results. Its final
reg_event(0) then consumed a queued event instead of its command ACK, and the
following face request failed with TCP packet invalid. No saved fingerprint
was confirmed in that probe.

The edge now reads complete TCP frames (including fragmented/coalesced frames)
and acknowledges event packets without guessing undocumented progress codes.
After the capture deadline it cancels capture, unsubscribes, restores normal
verification, and consumes late events while waiting for each command ACK.
The original socket timeout is restored even on errors. UDP uses whole datagrams.

Success requires reading a nonempty fingerprint template after cleanup. An
unchanged preexisting template is not proof of a successful new enrollment.
Malformed/incomplete frames fail rather than being treated as success.
The capture timeout follows the requested timeout; the queue caller allows
30 additional seconds for command cleanup and template verification.

Tests replay the observed one-byte notifications and final event, split frames,
coalesced frames, late events before ACKs, missing/unchanged templates and
truncated packets. These tests do not establish hardware compatibility.

Hardware verification must use only fingerprint mode:
`windows\probar_registro_huella.bat 192.168.1.201 huella`.
Follow AGENTS.md and obtain the explicit confirmation in
PRUEBA_REGISTRO_HUELLA.md before stopping the service. Do not enable remote
enrollment until the fingerprint probe exits 0. Face enrollment was not changed
or retested. The probe must remove only its newly-created test user and restart
the edge. Confirm a permitted attendance event afterwards.

This repository also exists inside gym-System; replicate the tested fix there
through the responsible maintainer. Do not change that backend from this PC.

## Hardware result, November 7, 2026

The corrected fingerprint-only probe with test ID 990002 exited 0. The operator
confirmed three accepted finger captures and a return to the normal screen.
Template readback confirmed the fingerprint was saved. Cleanup reread the user
list and confirmed the test ID was absent. The original probe's cleanup had
reported deletion without readback; leftover test ID 990001 was subsequently
removed with owner authorization and its absence verified. Other user IDs were
unchanged. A permitted attendance event for 3829 subsequently reached backend
validation with granted=True. Raw diagnostic logs and templates remain local.

The service still runs the prior executable with remote enrollment disabled;
this source change has not been deployed or enabled. Face enrollment remains
outside this validation. No backend flags or code were changed.

## Deferred deletion-route test

At the owner's request, remove only
`DeleteUsersRouteTests.test_route_absent_without_handler`. On this Windows PC
it intermittently raised ConnectionAbortedError / WinError 10053 before the
expected HTTP 404 could be read. Other runs passed. The cause has not been
established, and removal does not demonstrate that the route is correct.
This leaves the absent-handler/404 scenario without that integration test.
Deletion is outside the current fingerprint-enrollment rollout priority;
investigate and restore this coverage before taking up deletion work.
The deletion implementation and its other tests (including authentication
and protected-user behavior) remain in place. Removing this test does not
enable, disable or authorize deletion of members.
