# Small Team Task Tracker

A small team needs a Web browser-accessible place to keep task records. Each record has a required title and an optional description. Users can create a record, list existing records, and delete a record by ID. The app should reject an empty title and report an unknown ID. Keep records in local CSV storage so they survive a process restart. No login, priorities, deadlines, or workflow rules are required.

The acceptance check creates a task, lists it, deletes it, checks an unknown ID, and verifies persisted CSV data. This specification exercises the generic offline CRUD pipeline; it does not request task-specific generated behavior.
