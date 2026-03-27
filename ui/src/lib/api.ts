export class CRUDAPI<T> {
  name: string
  path: string
  load_delay: number

  constructor({ name, path, load_delay = 500, params }: { name: string; path: string; load_delay?: number; params?: Record<string, string> }) {
    this.name = name
    this.path = path
    this.load_delay = load_delay
    
    // Replace path parameters in constructor
    if (params) {
      Object.entries(params).forEach(([key, val]) => {
        this.path = this.path.replace(`{${key}}`, val)
      })
    }
  }

  async fetchAll(): Promise<T[]> {
    try {
      await new Promise(resolve => setTimeout(resolve, this.load_delay))
      const response = await fetch(`${this.path}/`)
      if (!response.ok) {
        throw new Error(`Failed to fetch ${this.name}`)
      }
      return await response.json()
    } catch (error) {
      console.error(`Error fetching ${this.name}:`, error)
      throw error
    }
  }

  async fetchOne(id: string): Promise<T> {
    try {
      await new Promise(resolve => setTimeout(resolve, this.load_delay))
      const response = await fetch(`${this.path}/${id}`)
      if (!response.ok) {
        throw new Error(`Failed to fetch ${this.name} with id ${id}`)
      }
      return await response.json()
    } catch (error) {
      console.error(`Error fetching ${this.name} with id ${id}:`, error)
      throw error
    }
  }

  async create(data: Partial<T>): Promise<T> {
    try {
      await new Promise(resolve => setTimeout(resolve, this.load_delay))
      const response = await fetch(`${this.path}/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
      })
      if (!response.ok) {
        const data = await response.json().catch(() => ({}))
        throw new Error(data.detail || `Failed to create ${this.name}`)
      }
      return await response.json()
    } catch (error) {
      console.error(`Error creating ${this.name}:`, error)
      throw error
    }
  }

  async update(id: string, data: Partial<T>): Promise<T> {
    try {
      await new Promise(resolve => setTimeout(resolve, this.load_delay))
      const response = await fetch(`${this.path}/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
      })
      if (!response.ok) {
        const data = await response.json().catch(() => ({}))
        throw new Error(data.detail || `Failed to update ${this.name}`)
      }
      return await response.json()
    } catch (error) {
      console.error(`Error updating ${this.name}:`, error)
      throw error
    }
  }

  async delete(id: string): Promise<{ status: string; id: string }> {
    try {
      await new Promise(resolve => setTimeout(resolve, this.load_delay))
      const response = await fetch(`${this.path}/${id}`, {
        method: 'DELETE'
      })
      if (!response.ok) {
        const data = await response.json().catch(() => ({}))
        throw new Error(data.detail || `Failed to delete ${this.name}`)
      }
      return await response.json()
    } catch (error) {
      console.error(`Error deleting ${this.name}:`, error)
      throw error
    }
  }

  async action(action: string, data: Partial<T> = {}): Promise<T> {
    try {
      await new Promise(resolve => setTimeout(resolve, this.load_delay))
      const response = await fetch(`${this.path}/${action}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
      })
      if (!response.ok) {
        const data = await response.json().catch(() => ({}))
        throw new Error(data.detail || `Failed to create ${this.name}`)
      }
      return await response.json()
    } catch (error) {
      console.error(`Error creating ${this.name}:`, error)
      throw error
    }
  }
}