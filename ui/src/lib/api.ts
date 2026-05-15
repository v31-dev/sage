export interface APIError extends Error {
  response?: Response
  status?: number
}

function createAPIError(message: string, response: Response): APIError {
  const error = new Error(message) as APIError
  error.response = response
  error.status = response.status
  return error
}

export class CRUDAPI<T> {
  name: string
  path: string
  load_delay: number

  constructor({
    name,
    path,
    load_delay = 500,
    params,
  }: {
    name: string
    path: string
    load_delay?: number
    params?: Record<string, string>
  }) {
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
        const data = await response.json().catch(() => ({}))
        throw createAPIError(data.detail || `Failed to fetch ${this.name}`, response)
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
        const data = await response.json().catch(() => ({}))
        throw createAPIError(data.detail || `Failed to fetch ${this.name} with id ${id}`, response)
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
        body: JSON.stringify(data),
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
        body: JSON.stringify(data),
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

  async delete(
    id: string,
    queryParams?: Record<string, string | number | boolean | null | undefined>
  ): Promise<{ status: string; id: string }> {
    try {
      await new Promise(resolve => setTimeout(resolve, this.load_delay))
      const params = new URLSearchParams()
      if (queryParams) {
        Object.entries(queryParams).forEach(([key, value]) => {
          if (value !== null && value !== undefined) {
            params.set(key, String(value))
          }
        })
      }

      const queryString = params.toString()
      const response = await fetch(`${this.path}/${id}${queryString ? `?${queryString}` : ''}`, {
        method: 'DELETE',
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
        body: JSON.stringify(data),
      })
      if (!response.ok) {
        const data = await response.json().catch(() => ({}))
        const error = createAPIError(data.detail || `Failed to create ${this.name}`, response)
        throw error
      }
      const result = await response.json()
      ;(result as any).response = response
      return result
    } catch (error) {
      console.error(`Error creating ${this.name}:`, error)
      throw error
    }
  }
}
