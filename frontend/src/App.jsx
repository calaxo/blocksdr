import { Suspense } from 'react'
import { useRoutes } from 'react-router-dom'
import routes from '~react-pages'
import Toolbar from '@/components/Toolbar'

export default function App() {
  const element = useRoutes(routes)
  return (
    <div className="h-full flex flex-col">
      <Toolbar />
      <Suspense fallback={<div className="text-mut p-6">Chargement…</div>}>
        {element}
      </Suspense>
    </div>
  )
}
