Routing eval, 24 labelled requests, model: gateway

Label accuracy: 0.92

| hand label | requests | labelled right | router labels |
|---|---:|---:|---|
| lookup | 6 | 6 | lookup 6 |
| search | 6 | 6 | search 6 |
| hard | 6 | 4 | hard 4, lookup 1, out_of_scope 1 |
| out_of_scope | 6 | 6 | out_of_scope 6 |

| policy | cost per request (CAD) | total (CAD) | large-model calls | of which router (CAD) |
|---|---:|---:|---:|---:|
| all-large (before routing) | 0.00011079 | 0.002659 | 24 | 0 |
| routed | 0.00003512 | 0.000843 | 5 | 0.000171 |

Routed cost is 0.32 of all-large.
Large-model calls on requests labelled out_of_scope: 0

Labelled differently from the hand label:
- r15 hand hard, router out_of_scope, ran out_of_scope: Can I return boots I wore once on a hike?
- r17 hand hard, router lookup, ran lookup: If I add a water bottle to order 1043, do I still get free shipping and how much more do I pay?
