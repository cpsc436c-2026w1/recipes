Feature: Clean-up removes what the recipe created and keeps what it borrowed
  Offline, in moto: the whole set is made as the live run makes it, then removed.

  @R130
  Rule: clean_up.py deletes what the ledger records created, lake.py down goes last, and nothing is left

    Example: Everything made, then removed
      Given the lake bucket, the shared workgroup, the stream, the check and its alarm were made
      When clean_up.py runs without --yes
      Then it lists what it would remove and deletes nothing
      When clean_up.py --yes runs and then scripts/lake.py down --yes
      Then clean_up.py --check finds nothing left by schedule group or by name prefix

    Example: A run log that was there before is kept
      Given a run log recipe-runs that another recipe made
      When clean_up.py --yes runs
      Then the table recipe-runs is still there

  @R131
  Rule: clean_up.py deletes only this recipe's own files under raw/trip-events/

    Example: Another recipe's replay files are kept
      Given the lake bucket holds a slot file of this recipe's replay "mine-s3" and one of another recipe's replay
      When clean_up.py --yes runs
      Then only the other recipe's slot file is left under raw/trip-events/
