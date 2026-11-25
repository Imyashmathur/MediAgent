import {randomUUID} from 'node:crypto';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {resolve} from 'node:path';
import {expect, test, type Page} from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

function syntheticPdf(): Buffer {
  const stream='BT /F1 16 Tf 72 730 Td (Laboratory report) Tj 0 -24 Td (Test name: Synthetic glucose) Tj 0 -24 Td (Observed value: 6.2) Tj ET';
  const objects=[
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>',
    `<< /Length ${Buffer.byteLength(stream)} >>\nstream\n${stream}\nendstream`,
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
  ];
  let pdf='%PDF-1.4\n'; const offsets=[0];
  for(const [index,object] of objects.entries()) {offsets.push(Buffer.byteLength(pdf)); pdf+=`${index+1} 0 obj\n${object}\nendobj\n`;}
  const xref=Buffer.byteLength(pdf);
  pdf+=`xref\n0 ${offsets.length}\n0000000000 65535 f \n`;
  for(const offset of offsets.slice(1)) pdf+=`${String(offset).padStart(10,'0')} 00000 n \n`;
  pdf+=`trailer\n<< /Size ${offsets.length} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(pdf);
}

async function registerPatient(page:Page):Promise<void> {
  await page.goto('/');
  await page.getByRole('button',{name:'Create a patient or doctor account'}).click();
  await page.getByLabel('Full name').fill('Synthetic Patient');
  await page.getByLabel('Email').fill(`browser-${randomUUID()}@example.test`);
  await page.getByLabel('Password').fill('synthetic-password-123');
  await page.getByRole('button',{name:'Create account',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Hello, Synthetic Patient'})).toBeVisible();
}

const execFileAsync=promisify(execFile);
async function seedAdmin(email:string, password:string) {
  const code='import asyncio,sys; from app.create_admin import create_admin; asyncio.run(create_admin(sys.argv[1],"Synthetic Admin",sys.argv[2]))';
  await execFileAsync('docker',['compose','exec','-T','api','python','-c',code,email,password],{cwd:resolve(process.cwd(),'..')});
}
async function disableAdmin(email:string) {
  const code='import asyncio,sys; from sqlalchemy import update; from app.db import Session; from app.models import User\nasync def f():\n async with Session() as db:\n  await db.execute(update(User).where(User.email==sys.argv[1],User.role=="admin").values(active=False)); await db.commit()\nasyncio.run(f())';
  await execFileAsync('docker',['compose','exec','-T','api','python','-c',code,email],{cwd:resolve(process.cwd(),'..')});
}

test('authentication screen supports keyboard navigation and has no automated WCAG A/AA violations', async({page})=>{
  await page.goto('/');
  await page.keyboard.press('Tab');
  await expect(page.getByRole('link',{name:/MediAgent/})).toBeFocused();
  const result=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21a','wcag21aa']).analyze();
  expect(result.violations.map(v=>({id:v.id,targets:v.nodes.map(n=>n.target)}))).toEqual([]);
});

test('patient can upload, process, isolate, and delete a synthetic report', async({page,browser})=>{
  await page.route('**/api/document-processing/capabilities',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({vision_available:true,provider:'ollama',model:'synthetic-vision',service_url:'http://synthetic.test',configuration:'a'.repeat(64),max_pages:3})}));
  await registerPatient(page);
  const accessibility=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21a','wcag21aa']).analyze();
  expect(accessibility.violations.map(v=>({id:v.id,targets:v.nodes.map(n=>n.target)}))).toEqual([]);
  const upload=page.locator('section').filter({has:page.getByRole('heading',{name:'Upload a document'})});
  await upload.getByLabel('Medical document').setInputFiles({name:'synthetic-report.pdf',mimeType:'application/pdf',buffer:syntheticPdf()});
  await upload.getByRole('button',{name:'Upload document'}).click();
  const row=page.locator('.row').filter({hasText:'synthetic-report.pdf'});
  await expect(row).toBeVisible();
  const href=await row.getByRole('link',{name:'Download'}).getAttribute('href');
  expect(href).toMatch(/^\/api\/documents\/[0-9a-f-]+\/download$/);
  const docId=href!.split('/')[3];
  const otherContext=await browser.newContext();
  try {
    const other=await otherContext.newPage();
    await registerPatient(other);
    expect((await other.request.get(`/api/documents/${docId}/download`)).status()).toBe(403);
  } finally {await otherContext.close();}
  await row.getByRole('button',{name:'View processing'}).click();
  const processing=page.locator('article').filter({has:page.getByRole('heading',{name:'Document processing'})});
  const vision=processing.getByRole('checkbox',{name:/Also send up to the first three page images/});
  await expect(vision).toBeVisible();
  await expect(processing.getByText(/synthetic-vision at http:\/\/synthetic.test/)).toBeVisible();
  await vision.check();
  const request=page.waitForRequest(r=>r.url().endsWith(`/api/documents/${docId}/processing`) && r.method()==='POST');
  await processing.getByRole('button',{name:'Process document',exact:true}).click();
  expect((await request).postDataJSON()).toEqual({vision_requested:true,vision_config_fingerprint:'a'.repeat(64)});
  await expect(page.getByRole('alert').filter({hasText:'Vision provider is not configured'})).toBeVisible();
  await vision.uncheck();
  await processing.getByRole('button',{name:'Process document',exact:true}).click();
  await expect.poll(async()=>{
    await processing.getByRole('button',{name:'Refresh status'}).click();
    return await processing.textContent();
  },{timeout:40_000,intervals:[500,1000]}).toContain('Result: completed');
  await expect(processing.getByText('Synthetic glucose',{exact:false}).first()).toBeVisible();
  await expect(processing.getByText('Document type hint · unverified: laboratory report · page 1: “Laboratory report”')).toBeVisible();
  const processedAccessibility=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21a','wcag21aa']).analyze();
  expect(processedAccessibility.violations.map(v=>({id:v.id,targets:v.nodes.map(n=>n.target)}))).toEqual([]);
  await row.locator('summary').click();
  await row.getByLabel('Type DELETE to confirm').fill('DELETE');
  await row.getByRole('button',{name:'Request deletion'}).click();
  await expect(row).toHaveCount(0);
  expect((await page.request.get(`/api/documents/${docId}/download`)).status()).toBe(404);
  const deletions=page.locator('section').filter({has:page.getByRole('heading',{name:'Document deletion status'})});
  await expect.poll(async()=>{
    await deletions.getByRole('button',{name:'Refresh deletion status'}).click();
    return await deletions.textContent();
  },{timeout:20_000,intervals:[500,1000]}).toContain('Local file removed');
  const refreshCookie=(await page.context().cookies()).find(cookie=>cookie.name==='mediagent_refresh');
  expect(refreshCookie).toBeTruthy();
  await page.context().clearCookies({name:'mediagent_access'});
  await page.getByRole('button',{name:'Sign out'}).click();
  await expect(page.getByRole('heading',{name:'Welcome back'})).toBeVisible();
  const oldRefresh=await page.request.post('http://localhost:8100/auth/refresh',{data:{refresh_token:refreshCookie!.value}});
  expect(oldRefresh.status()).toBe(401);
});

test('verified doctor reviews a patient transcription and approves a note', async({browser})=>{
  test.skip(process.env.MEDIAGENT_E2E_ADMIN_SEED !== '1', 'requires explicit local synthetic administrator seeding');
  const adminEmail=`browser-admin-${randomUUID()}@example.test`;
  const password=`synthetic-${randomUUID()}-${randomUUID()}`;
  const contexts=await Promise.all([browser.newContext(),browser.newContext(),browser.newContext()]);
  try {
    await seedAdmin(adminEmail,password);
    const [patient,doctor,admin]=await Promise.all(contexts.map(context=>context.newPage()));
    await doctor.goto('/');
    await doctor.getByRole('button',{name:'Create a patient or doctor account'}).click();
    await doctor.getByLabel('Full name').fill('Synthetic Doctor');
    await doctor.getByLabel('Account type').selectOption('doctor');
    await doctor.getByLabel('Email').fill(`browser-doctor-${randomUUID()}@example.test`);
    await doctor.getByLabel('Password').fill(password);
    await doctor.getByRole('button',{name:'Create account',exact:true}).click();
    await expect(doctor.getByRole('heading',{name:'Hello, Synthetic Doctor'})).toBeVisible();
    const doctorId=await doctor.locator('.identity code').textContent();
    expect(doctorId).toBeTruthy();
    await admin.goto('/');
    await admin.getByLabel('Email').fill(adminEmail);
    await admin.getByLabel('Password').fill(password);
    await admin.getByRole('button',{name:'Sign in',exact:true}).click();
    const verification=admin.locator('section').filter({has:admin.getByRole('heading',{name:'Review clinician profile'})});
    await verification.getByLabel('Doctor ID').fill(doctorId!);
    await verification.getByRole('button',{name:'View profile'}).click();
    await verification.getByRole('button',{name:'Mark verified'}).click();
    await expect(verification.getByRole('status')).toHaveText('Verification decision recorded.');
    const adminAccessibility=await new AxeBuilder({page:admin}).withTags(['wcag2a','wcag2aa','wcag21a','wcag21aa']).analyze();
    expect(adminAccessibility.violations.map(v=>({id:v.id,targets:v.nodes.map(n=>n.target)}))).toEqual([]);
    await registerPatient(patient);
    const patientId=await patient.locator('.identity code').textContent();
    expect((await doctor.request.get(`/api/patients/${patientId}/documents`)).status()).toBe(403);
    expect((await admin.request.get(`/api/patients/${patientId}/documents`)).status()).toBe(403);
    expect((await admin.request.get(`/api/patients/${patientId}/notes`)).status()).toBe(403);
    const relationship=patient.locator('section').filter({has:patient.getByRole('heading',{name:'Add a care team member'})});
    await relationship.getByLabel('Doctor ID').fill(doctorId!);
    await relationship.getByLabel('Purpose').fill('Synthetic care');
    await relationship.getByRole('button',{name:'Create relationship'}).click();
    const grant=patient.locator('section').filter({has:patient.getByRole('heading',{name:'Grant access'})});
    await expect(grant.getByLabel('Care relationship').locator(`option[value]:not([disabled])`)).toHaveCount(1);
    for(const scope of ['read_documents','review_documents','clinical_notes']) {
      await grant.getByLabel('Care relationship').selectOption({label:doctorId!});
      await grant.getByLabel('Permission').selectOption(scope);
      await grant.getByLabel('Purpose').fill('Synthetic care');
      await grant.getByLabel('Expires on').fill(new Date(Date.now()+2*86400_000).toISOString().slice(0,16));
      const granted=patient.waitForResponse(response=>response.url().endsWith('/api/consents') && response.request().method()==='POST');
      await grant.getByRole('button',{name:'Grant access'}).click();
      expect((await granted).status()).toBe(201);
      await expect(patient.locator('section').filter({has:patient.getByRole('heading',{name:'Access permissions'})}).locator('.row')).toHaveCount(['read_documents','review_documents','clinical_notes'].indexOf(scope)+1);
    }
    const localDateTime=(timestamp:number)=>{const d=new Date(timestamp); return new Date(timestamp-d.getTimezoneOffset()*60_000).toISOString().slice(0,16);};
    await grant.getByLabel('Care relationship').selectOption({label:doctorId!});
    await grant.getByLabel('Permission').selectOption('assistant');
    await grant.getByLabel('Purpose').fill('Scheduled synthetic care');
    await grant.getByLabel('Starts on (optional)').fill(localDateTime(Date.now()+300_000));
    await grant.getByLabel('Expires on').fill(localDateTime(Date.now()+600_000));
    const scheduled=patient.waitForResponse(response=>response.url().endsWith('/api/consents') && response.request().method()==='POST');
    await grant.getByRole('button',{name:'Grant access'}).click();
    expect((await scheduled).status()).toBe(201);
    const scheduledRow=patient.locator('section').filter({has:patient.getByRole('heading',{name:'Access permissions'})}).locator('.row').filter({hasText:'Scheduled synthetic care'});
    await expect(scheduledRow).toContainText('not yet effective');
    await scheduledRow.getByRole('button',{name:'Withdraw'}).click();
    await expect(scheduledRow).toContainText('withdrawn');
    const upload=patient.locator('section').filter({has:patient.getByRole('heading',{name:'Upload a document'})});
    await upload.getByLabel('Medical document').setInputFiles({name:'clinician-report.pdf',mimeType:'application/pdf',buffer:syntheticPdf()});
    await upload.getByRole('button',{name:'Upload document'}).click();
    const patientRow=patient.locator('.row').filter({hasText:'clinician-report.pdf'});
    await expect(patientRow).toBeVisible();
    await patientRow.getByRole('button',{name:'View processing'}).click();
    const patientProcessing=patient.locator('article').filter({has:patient.getByRole('heading',{name:'Document processing'})});
    await patientProcessing.getByRole('button',{name:'Process document',exact:true}).click();
    await expect.poll(async()=>{await patientProcessing.getByRole('button',{name:'Refresh status'}).click(); return await patientProcessing.textContent();},{timeout:40_000,intervals:[500,1000]}).toContain('Result: completed');
    await doctor.reload();
    const workspace=doctor.locator('section').filter({has:doctor.getByRole('heading',{name:'Patient workspace'})});
    await workspace.getByLabel('Patient ID').fill(patientId!);
    let releaseDocuments!:()=>void, documentRequested!:()=>void, documentFulfilled!:()=>void;
    const documentGate=new Promise<void>(resolve=>{releaseDocuments=resolve;});
    const documentSeen=new Promise<void>(resolve=>{documentRequested=resolve;});
    const documentDone=new Promise<void>(resolve=>{documentFulfilled=resolve;});
    const documentPattern=`**/api/patients/${patientId}/documents`;
    await doctor.route(documentPattern,async route=>{documentRequested(); await documentGate; await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify([{id:'stale-doc',filename:'Stale patient document',content_type:'application/pdf',review_status:'unverified',scan_status:'not_scanned'}])}); documentFulfilled();});
    await workspace.getByRole('button',{name:'Open patient documents'}).click();
    await documentSeen;
    await workspace.getByLabel('Patient ID').fill('another-patient');
    releaseDocuments();
    await documentDone;
    await doctor.unroute(documentPattern);
    await expect(doctor.getByText('Stale patient document')).toHaveCount(0);
    await workspace.getByLabel('Patient ID').fill(patientId!);
    await workspace.getByRole('button',{name:'Open patient documents'}).click();
    await expect(doctor.locator('.row').filter({hasText:'clinician-report.pdf'})).toBeVisible();
    await expect(doctor.getByText('Stale patient document')).toHaveCount(0);
    await doctor.locator('.row').filter({hasText:'clinician-report.pdf'}).getByRole('button',{name:'View processing'}).click();
    const doctorProcessing=doctor.locator('article').filter({has:doctor.getByRole('heading',{name:'Document processing'})});
    await doctorProcessing.getByLabel('Observed value').fill('6.3');
    await doctorProcessing.getByRole('button',{name:'Save clinician review'}).click();
    await expect(doctorProcessing.getByText('Structured transcription · reviewed')).toBeVisible();
    const doctorAccessibility=await new AxeBuilder({page:doctor}).withTags(['wcag2a','wcag2aa','wcag21a','wcag21aa']).analyze();
    expect(doctorAccessibility.violations.map(v=>({id:v.id,targets:v.nodes.map(n=>n.target)}))).toEqual([]);
    await workspace.getByRole('button',{name:'Open clinical notes'}).click();
    const notes=doctor.locator('section').filter({has:doctor.getByRole('heading',{name:'Clinical notes'})});
    await notes.getByLabel('New note draft').fill('Synthetic clinician reviewed note');
    await notes.getByLabel('Source document (optional)').selectOption({label:'clinician-report.pdf'});
    await notes.getByRole('checkbox',{name:/Link the currently open completed processing run/}).check();
    await notes.getByRole('checkbox',{name:/Also link the displayed transcription version/}).check();
    await notes.getByRole('checkbox',{name:/Laboratory test 1 · observed value: 6.3/}).check();
    await notes.getByLabel('Exact note statement for Laboratory test 1 · observed value (optional)').fill('Synthetic clinician reviewed note');
    await notes.getByRole('button',{name:'Create draft'}).click();
    const note=notes.locator('article').filter({hasText:'Synthetic clinician reviewed note'});
    await expect(note.getByRole('link',{name:'Download source document'})).toBeVisible();
    await expect(note).toContainText('transcription version 2');
    await expect(note).toContainText('test 1 observed value → “Synthetic clinician reviewed note”');
    await doctorProcessing.getByLabel('Observed value').fill('6.4');
    await doctorProcessing.getByRole('button',{name:'Save clinician review'}).click();
    await expect(doctorProcessing.getByText('Reviewed by', {exact:false}).first()).toContainText('version 3');
    await note.getByRole('button',{name:'Open cited run'}).click();
    await expect(doctorProcessing.getByRole('heading',{name:/cited version 2/})).toBeVisible();
    await expect(doctorProcessing.locator('.extraction-field').filter({hasText:'Observed value'}).getByText('6.3',{exact:true})).toBeVisible();
    await expect(doctorProcessing.getByRole('button',{name:'Save clinician review'})).toHaveCount(0);
    await note.getByRole('button',{name:'Submit for review'}).click();
    await note.getByRole('button',{name:'Approve',exact:true}).click();
    await expect(note.getByText('Version 1 · approved')).toBeVisible();
    await patient.reload();
    await expect(patient.getByText('Synthetic clinician reviewed note',{exact:true})).toBeVisible();
    const permissions=patient.locator('section').filter({has:patient.getByRole('heading',{name:'Access permissions'})});
    await permissions.locator('.row').filter({hasText:'Clinical notes · active'}).getByRole('button',{name:'Withdraw'}).click();
    await expect(patient.getByText('Synthetic clinician reviewed note',{exact:true})).toBeVisible();
    await workspace.getByRole('button',{name:'Open clinical notes'}).click();
    await expect(doctor.getByRole('alert').filter({hasText:'Patient access is not authorized'})).toBeVisible();
    const careTeam=patient.locator('section').filter({has:patient.getByRole('heading',{name:'Care team relationships'})});
    const careRow=careTeam.locator('.row').filter({hasText:doctorId!});
    await careRow.getByRole('button',{name:'End relationship'}).click();
    await expect(careRow.getByRole('button',{name:'Restore relationship'})).toBeVisible();
    await careRow.getByRole('button',{name:'Restore relationship'}).click();
    await expect(careRow.getByRole('button',{name:'End relationship'})).toBeVisible();
    await expect(permissions.locator('.row').filter({hasText:'Documents · withdrawn'})).toBeVisible();
    const denied=await doctor.request.get(`/api/patients/${patientId}/documents`);
    expect(denied.status()).toBe(403);
  } finally {
    await Promise.all(contexts.map(context=>context.close()));
    await disableAdmin(adminEmail);
  }
});
