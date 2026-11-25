import React from 'react';
import {afterEach, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen} from '@testing-library/react';
import EmailVerification from '../../app/email-verification';
import VerifyEmailPage from '../../app/verify-email/page';

afterEach(()=>{cleanup(); window.history.replaceState(null,'','/'); vi.unstubAllGlobals();});

it('requests a verification link and exposes refresh', async()=>{
  const api=vi.fn(async()=>({} as Response));
  const refresh=vi.fn(async()=>{});
  render(<EmailVerification verified={false} api={api} onRefresh={refresh} onError={vi.fn()}/>);
  fireEvent.click(screen.getByRole('button',{name:'Send verification link'}));
  await screen.findByText('Verification link queued. Check the configured mail inbox.');
  expect(api).toHaveBeenCalledWith('auth/email-verification/request',{method:'POST'});
  fireEvent.click(screen.getByRole('button',{name:'Refresh email status'}));
  expect(refresh).toHaveBeenCalledOnce();
});

it('clears the link fragment and requires an explicit confirmation', async()=>{
  window.history.replaceState(null,'','/verify-email#token=synthetic-token');
  const fetcher=vi.fn(async()=>({ok:true,json:async()=>({detail:'Email address verified.'})}));
  vi.stubGlobal('fetch',fetcher);
  render(<VerifyEmailPage/>);
  expect(window.location.hash).toBe('');
  expect(fetcher).not.toHaveBeenCalled();
  fireEvent.click(await screen.findByRole('button',{name:'Verify email address'}));
  await screen.findByText('Email address verified.');
  expect(fetcher).toHaveBeenCalledWith('/api/auth/email-verification/confirm',expect.objectContaining({method:'POST',body:JSON.stringify({token:'synthetic-token'})}));
});
