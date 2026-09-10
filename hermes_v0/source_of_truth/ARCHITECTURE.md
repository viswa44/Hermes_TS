agents working architecture model.

                    HERMES CONTROL PLANE
                           │
                           ▼
                     TASK QUEUE
                           │
                           ▼
                     STATE MACHINE
                           │
                           ▼
                  ┌─────────────────┐
                  │ ONE ACTIVE AGENT│
                  └────────┬────────┘
                           │
                           ▼
                    CODEX CLI / VS CODE
                           │
                           ▼
                      WORK + LOG
                           │
                           ▼
                       HANDOFF
                           │
                           ▼
                    HUMAN APPROVAL
                           │
                           ▼
                    NEXT AGENT