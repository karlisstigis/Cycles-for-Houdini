# Ship the RIS direct-light sampler

The [fork policy](0005-cycles-fork-policy.md) defines the OptiX comparison and the release decision for this sampler.

Reservoir Light Sampling (RLS) is a production option carried in the locked engine patches. The normal build includes its integration sources and controls. Its validation and the standard-sampler control belong with the production engine. Bidirectional path tracing and the other research integrators use a separate experiment branch. [Locked patch contracts](../renderer-patch-contracts.md)
