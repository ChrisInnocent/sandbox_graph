"""Trusted required-gate planning over the actual frozen diff classification."""
from ..runtime.candidate_workspace import classify_diff
from ..domain.models import canonical_hash

GATES=('G0','G1','G2','G3','G4','G6.pre')


def gate_plan(candidate,profile):
    actual=classify_diff(candidate.changed_paths)
    if int(candidate.risk[1:])<int(actual[1:]):raise ValueError('candidate understates actual diff risk')
    # G5 is the independent review/publication decision over this immutable test
    # manifest; G6.post belongs to the activated run, avoiding circular proofs.
    required=('G0',) if candidate.risk=='R0' else GATES
    mapping=profile.get('cases',{})
    if set(mapping)-set(GATES):raise ValueError('unknown trusted gate profile entry')
    cases=[]
    for gate in required:
        ids=mapping.get(gate)
        if not isinstance(ids,list) or not ids or any(not isinstance(x,str) or not x for x in ids):
            raise ValueError('trusted required gate has no fixed case IDs: '+gate)
        if len(set(ids))!=len(ids):raise ValueError('duplicate trusted case ID')
        cases.extend(ids)
    if len(set(cases))!=len(cases):raise ValueError('case IDs must be unique across gate assignments')
    result={'profile_ref':profile['profile_ref'],'required_gates':list(required),
        'expected_case_ids':sorted(cases),'case_assignments':{key:list(mapping[key]) for key in required},
        'trusted_suite_hash':profile['suite_hash'],'policy_hash':profile['policy_hash'],
        'candidate_ref':candidate.candidate_ref,'candidate_hash':candidate.candidate_hash}
    return result|{'plan_hash':canonical_hash(result)}
