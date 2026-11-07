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

## Early finish and face enrollment (follow-up)

In Norias the reader saved the fingerprint and, separately, a face, but the app
kept waiting: the edge only stopped at the capture deadline
(`ZK_ENROLL_TIMEOUT_SECONDS`, 240 s) plus 30 s, and live access events were
not captured meanwhile. The face request ended with RL-401 after 4.5 minutes
without checking whether the face was saved.

`edge_service_zkteco/zk_enroll.py` now handles both:

- The capture ends at the final enrollment event. Observed layout: result (H),
  size (H), user_id (`0000 4e03 "990001"...`). Result 0 = saved.
- Attendance events (user_id at the start, or pyzk attendance sizes) that
  arrive while the person stays in front of the reader do not count as
  enrollment progress.
- Face uses the same complete-frame reading and cleanup as the fingerprint.
  It is saved when the final event has result 0 (this also covers replacing an
  existing face, where the face count does not grow) or when the face count
  grows. Without a final event it stops after 8 s without progress events.
- Fingerprint success still requires reading a new or different template.

The face final event has not been observed yet: verify with
`windows\probar_registro_huella.bat 192.168.1.201 cara` and keep the packets
printed with `--verbose`.

## Deletion-route test restored

`DeleteUsersRouteTests.test_route_absent_without_handler` and
`test_unknown_path_returns_404` failed intermittently on Windows with
WinError 10053. The control server answered 404/401 without reading the
request body; closing a socket with unread data makes Windows send RST, so the
client lost the response. The server now reads the body before any response
and the removed test is back.
