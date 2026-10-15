# DECISIONS.md

1. **Indexes.** On attendance I made a unique (emp_code, date) index, so there can be only one record per person per day. I also made indexes for the list order (date, emp_code), the status filter, and the late leaderboard. On employees I made a unique emp_code index and one on department and joined date. I did not index punch_in, because punch-out only looks at one person's few records, so it would just slow down writes.

2. **Two identical punch-ins at once.** Both try to insert. The unique index lets MongoDB accept only one. The other gets a duplicate error and I return 409.

3. **Ties at the leaderboard cutoff.** I rank first (1, 2, 2, 4) and only then keep rank up to the limit. So everyone tied at the cutoff is returned, even if that is more rows than the limit.

4. **Headcount with no logs.** The summary starts from the employees collection, then joins their logs. A person with no logs still appears, so they are counted.

5. **100 times more data.** I would save monthly totals per employee when records change, instead of adding up raw records on every request.
