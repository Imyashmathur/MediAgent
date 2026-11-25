import React from 'react';
import {afterEach, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen} from '@testing-library/react';
import ConsentHistory, {Consent, grantState} from '../../app/consent-history';

afterEach(cleanup);
const base:Consent = {id:'grant',relationship_id:'rel',doctor_id:'doctor-id',relationship_status:'active',purpose:'Care',scope:'read_documents',status:'active',effective_at:100,expires_at:200,withdrawn_at:null};

it('distinguishes consent state from dates and relationship state', ()=>{
  expect(grantState(base,150)).toBe('active');
  expect(grantState(base,200)).toBe('expired');
  expect(grantState(base,99)).toBe('not yet effective');
  expect(grantState({...base,relationship_status:'ended'},150)).toBe('relationship ended');
  expect(grantState({...base,status:'withdrawn',relationship_status:'ended'},250)).toBe('withdrawn');
});

it('shows the doctor and expired history and permits withdrawal', ()=>{
  const onWithdraw=vi.fn(), onRefresh=vi.fn();
  render(<ConsentHistory consents={[base]} onWithdraw={onWithdraw} onRefresh={onRefresh}/>);
  expect(screen.getByText(/Documents · expired/)).toBeTruthy();
  expect(screen.getByText(/Doctor ID: doctor-id/)).toBeTruthy();
  fireEvent.click(screen.getByRole('button',{name:'Withdraw'}));
  expect(onWithdraw).toHaveBeenCalledWith('grant');
  fireEvent.click(screen.getByRole('button',{name:'Refresh permissions'}));
  expect(onRefresh).toHaveBeenCalledOnce();
});
