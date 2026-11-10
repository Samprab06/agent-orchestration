# Team collaboration

GitHub Issues are the backlog. Read the pinned coordination issue before starting.
Sign board messages with your GitHub login and agent name. Mention the receiving
teammate on handoff and link the issue, branch, tests, and remaining work.

Agree on folder owners at kickoff and record them here once assigned. Get an
owner's agreement before editing their files. Claim a task in its issue and
check for an existing assignee or active branch before starting. Use a separate
branch per task, such as `agent/issue-123`.

Post blockers on the task issue and tag the teammate who can unblock it. Run
`python3 -m unittest -v` and include the results in a draft PR linked to the
issue. A human reviews and merges PRs.

Oracle execution requires an explicit approval from a configured collaborator.
The approval command covers the issue identity, title, and body. If the issue is
edited, generate a fresh command. Board mentions and labels only coordinate
work. Keep credentials in the operator's environment or a credential store.
