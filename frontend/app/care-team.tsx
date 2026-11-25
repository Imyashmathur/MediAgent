'use client';

type Relationship = {id:string; patient_id:string; doctor_id:string; status:string};
export default function CareTeam({relationships, role, onEnd, onRestore}:{relationships:Relationship[]; role:'patient'|'doctor'; onEnd:(id:string)=>void; onRestore:(id:string)=>void}) {
  return <section className="card"><h3>Care team relationships</h3><p>Ending a relationship stops access. Restoring one withdraws all earlier permissions; the patient must grant new permissions. Existing messages remain in the relationship and may be accessible again if message access is regranted.</p>{relationships.length ? relationships.map(r=><div className="row" key={r.id}><div><strong>{role === 'patient' ? 'Doctor' : 'Patient'} {role === 'patient' ? r.doctor_id : r.patient_id}</strong><p>{r.status}</p></div>{r.status === 'active' ? <button className="secondary" onClick={()=>onEnd(r.id)}>End relationship</button> : role === 'patient' && <button className="secondary" onClick={()=>onRestore(r.id)}>Restore relationship</button>}</div>) : <p>No care relationships.</p>}</section>;
}
