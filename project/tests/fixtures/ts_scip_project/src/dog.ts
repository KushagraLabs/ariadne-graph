import { Animal } from './animal';

export class Dog implements Animal {
  public sound(): string {
    return 'woof';
  }
}
