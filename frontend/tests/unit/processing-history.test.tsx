import React from 'react';
import {afterEach, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import ProcessingHistory from '../../app/processing-history';

afterEach(cleanup);

it('pages, selects and refreshes processing history', async()=>{
  let firstPage=0;
  const api=vi.fn(async (path:string)=>({json:async()=>path.includes('before=')
    ? {items:[{id:'old',status:'completed',created_at:10}],next_before:null}
    : ++firstPage === 1
      ? {items:[{id:'recent',status:'completed',created_at:20}],next_before:'recent'}
      : {items:[{id:'new',status:'queued',created_at:30}],next_before:null}} as Response));
  const onSelect=vi.fn();
  render(<ProcessingHistory api={api} documentId="document" onSelect={onSelect} onError={vi.fn()}/>);
  fireEvent.click(screen.getByText('Processing history'));
  fireEvent.click(await screen.findByRole('button',{name:'Load older runs'}));
  await waitFor(()=>expect(screen.getAllByRole('button',{name:'View run'})).toHaveLength(2));
  fireEvent.click(screen.getAllByRole('button',{name:'View run'})[1]);
  expect(onSelect).toHaveBeenCalledWith('old');
  expect(api).toHaveBeenCalledWith('documents/document/processing/history?before=recent');
  fireEvent.click(screen.getByRole('button',{name:'Refresh history'}));
  await screen.findByText(/queued/);
  expect(screen.getAllByRole('button',{name:'View run'})).toHaveLength(1);
});
