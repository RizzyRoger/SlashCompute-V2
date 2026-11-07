// Shared display helpers.

export const formatFlops = (n) => {
  if (n === null || n === undefined) return '-'
  const units = [[1e18, 'EFLOPs'], [1e15, 'PFLOPs'], [1e12, 'TFLOPs'], [1e9, 'GFLOPs']]
  const match = units.find(([size]) => Math.abs(n) >= size)
  return match ? `${(n / match[0]).toFixed(3)} ${match[1]}` : `${Math.round(n)} FLOPs`
}
