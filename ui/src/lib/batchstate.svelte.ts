/** App-wide state of the batch run (BatchView is the only writer). Settings that change what a
 *  save writes look at it: a batch saves with the settings it was created with (the server keeps
 *  a copy), so changes made while it runs apply to the next batch. */
export const batchRun = $state({ running: false })
