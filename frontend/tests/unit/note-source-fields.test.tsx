import React from 'react';
import {afterEach, expect, it} from 'vitest';
import {cleanup, fireEvent, render, screen} from '@testing-library/react';
import NoteSourceFields from '../../app/note-source-fields';

afterEach(cleanup);

it('selects a field by location without encoding its clinical value', ()=>{
  render(<form><NoteSourceFields data={{schema_version:'1.0',laboratory_tests:[{observed_value:{value:'synthetic result',origin:'clinician',evidence:null}}],prescription_items:[],warnings:[]}}/></form>);
  const input=screen.getByLabelText('Laboratory test 1 · observed value: synthetic result') as HTMLInputElement;
  fireEvent.click(input);
  expect(JSON.parse(input.value)).toEqual({group:'laboratory_tests',index:0,field:'observed_value'});
  expect(input.value).not.toContain('synthetic result');
  const quote=screen.getByLabelText('Exact note statement for Laboratory test 1 · observed value (optional)') as HTMLInputElement;
  fireEvent.change(quote,{target:{value:'Synthetic note statement'}});
  expect(quote.value).toBe('Synthetic note statement');
});
